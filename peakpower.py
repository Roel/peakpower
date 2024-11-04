# Peakpower: peak capacity notification buzzer
# Copyright (C) 2023-2024  Roel Huybrechts

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import datetime
from enum import Enum
import time
import logging
from threading import Thread
from influxdb import InfluxDBClient
from gpiozero import Device, Buzzer
import os

INFLUX_HOST = os.environ['INFLUX_HOST']
INFLUX_DB = os.environ['INFLUX_DB']
INFLUX_USER = os.environ['INFLUX_USER']
INFLUX_PASS = os.environ['INFLUX_PASS']

BUZZER_GPIO_PIN = int(os.environ['BUZZER_GPIO_PIN'])

debug = False

dbclient = InfluxDBClient(INFLUX_HOST, database=INFLUX_DB,
                          username=INFLUX_USER, password=INFLUX_PASS)

if debug:
    from gpiozero.pins.mock import MockFactory
    Device.pin_factory = MockFactory()
    logging.basicConfig(format='%(asctime)s - %(message)s',
                        level=logging.DEBUG)
else:
    logging.basicConfig(format='%(asctime)s - %(message)s',
                        level=logging.DEBUG)

now = datetime.datetime.now()
today = now.strftime("%Y-%m-%d")
first_day_of_month = datetime.date(now.year, now.month, 1).strftime("%Y-%m-%d")


def sleep():
    time.sleep(1 - time.monotonic() % 1)


class PeakBuzzer(Thread):

    class Alarm(Enum):
        IDLE = 0
        TEST = 99
        LEVEL_1 = 1
        LEVEL_2 = 2
        LEVEL_3 = 3

    def __init__(self):
        super().__init__()
        self.buzzer = Buzzer(BUZZER_GPIO_PIN)
        self.set_alarm(PeakBuzzer.Alarm.IDLE)

    def set_alarm(self, level):
        if level.value > 0:
            logging.info(f'Alarm set to {level}')
        else:
            logging.debug(f'Alarm set to {level}')

        self.state = level

    def run(self):
        while True:
            if self.state == PeakBuzzer.Alarm.IDLE:
                sleep()
                continue

            if self.state == PeakBuzzer.Alarm.TEST:
                logging.warning('ALARM firing: TEST')
                for i in range(3):
                    self.buzzer.on()
                    time.sleep(0.1)
                    self.buzzer.off()
                    time.sleep(0.1)

                self.set_alarm(PeakBuzzer.Alarm.IDLE)
                continue

            logging.warning(f'ALARM firing: {self.state}')
            for i in range(self.state.value):
                self.buzzer.on()
                time.sleep(0.5)
                self.buzzer.off()
                time.sleep(0.5)

            self.set_alarm(PeakBuzzer.Alarm.IDLE)


buzzer = PeakBuzzer()
buzzer.start()
buzzer.set_alarm(PeakBuzzer.Alarm.TEST)


def get_current_power():
    logging.debug('Getting current power')
    rs = dbclient.query(
        "select * from (SELECT value FROM p1_elec_power_fromgrid order by time desc limit 1),"
        "(SELECT value * -1 FROM p1_elec_power_togrid order by time desc limit 1) order by time desc"
    )

    results = []
    for r in rs.get_points():
        r['time'] = datetime.datetime.strptime(r['time'], '%Y-%m-%dT%H:%M:%SZ')
        results.append(r)

    results = sorted(results, key=lambda x: x['time'])
    current_power = results[-1]['value'] * 1000
    current_power = max(0, current_power)
    logging.debug(f'Current power is: {current_power}')
    return current_power


def get_current_peak():
    logging.debug('Getting current peak')
    rs = dbclient.query(
        "SELECT difference(last(value)) *4 as yield from p1_elec_total_fromgrid "
        f"where time > '{today}' and "
        f"time <= '{today}' + 1d "
        "group by rate, time(15m) tz('Europe/Brussels')"
    )

    rate1 = list(rs.get_points(tags={'rate': 'rate1'}))[-1]['yield']
    rate2 = list(rs.get_points(tags={'rate': 'rate2'}))[-1]['yield']
    current_peak = max(rate1, rate2) * 1000
    logging.debug(f'Current peak is: {current_peak}')
    return current_peak


def get_monthly_peak():
    logging.debug('Getting monthly peak')
    rs = dbclient.query(
        "SELECT difference(last(value)) *4 as yield from persist.p1_elec_total_fromgrid_max "
        f"where time >= '{first_day_of_month}' and "
        f"time <= '{today}' + 1d "
        "group by rate, time(15m) tz('Europe/Brussels')"
    )

    rate1 = max(i['yield'] for i in rs.get_points(tags={'rate': 'rate1'}))
    rate2 = max(i['yield'] for i in rs.get_points(tags={'rate': 'rate2'}))
    monthly_peak = max(rate1, rate2, 2.5) * 1000
    logging.debug(f'Monthly peak is: {monthly_peak}')
    return monthly_peak


monthly_peak = get_monthly_peak()

while True:
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    first_day_of_month = datetime.date(
        now.year, now.month, 1).strftime("%Y-%m-%d")

    if now.minute % 15 == 0 and (now.second-1) == 0:
        monthly_peak = get_monthly_peak()
        sleep()
        continue

    if now.hour >= 23 or now.hour <= 6:
        sleep()
        continue

    if now.minute % 15 < 5:
        sleep()
        continue

    if (now.second-1) % 30 != 0:
        sleep()
        continue

    total_seconds = 15 * 60
    seconds_passed = ((now.minute % 15) * 60) + now.second
    continuation_chance = 0.25 + (0.75*(seconds_passed/total_seconds))

    current_power = get_current_power()
    current_peak = get_current_peak()

    current_peak_estimate_linear = (current_peak/seconds_passed)*total_seconds
    current_peak_estimate_continuation = current_peak + \
        (current_power * (total_seconds-seconds_passed) / (3600/4))

    current_peak_estimate = (
        current_peak_estimate_linear + current_peak_estimate_continuation
        + (current_peak_estimate_continuation * continuation_chance)
    ) / (2 + continuation_chance)

    logging.debug(
        f'Current peak linear estimate is: {current_peak_estimate_linear}')
    logging.debug(
        f'Current peak continuation estimate is: {current_peak_estimate_continuation}')
    logging.debug(f'Current peak estimate is: {current_peak_estimate}')

    ratio = current_peak_estimate / monthly_peak
    logging.debug(f'Current monthly peak ratio is: {ratio}')
    if ratio < 0.9:
        alarm = PeakBuzzer.Alarm.IDLE
    elif ratio < 1:
        alarm = PeakBuzzer.Alarm.LEVEL_1
    elif ratio < 1.1:
        alarm = PeakBuzzer.Alarm.LEVEL_2
    else:
        alarm = PeakBuzzer.Alarm.LEVEL_3

    buzzer.set_alarm(alarm)

    sleep()
