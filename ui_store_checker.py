#!/usr/bin/env python3
# - coding: utf-8 --
import coloredlogs
import logging
import logging.handlers
import bz2
import inspect
import subprocess

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.chrome.options import Options

from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.utils import ChromeType

from selenium.webdriver.support import expected_conditions as EC
import time
import os
import sys
import random
import shutil

import yaml
import pprint
import pathlib
import datetime
import traceback
import csv

import notifier

CONFIG_LOGIN_PATH = "config.yaml"
CONFIG_TARGET_PATH = "target.yaml"

LOGIN_URL = "https://sso.ui.com/api/sso/v1/shopify_login"


CHROME_DATA_PATH = "chrome_data"
DUMP_PATH = "debug"
DATA_PATH = "data"
LOG_PATH = "log"
LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(filename)s:%(lineno)s %(funcName)s] %(message)s"
)
HISTORY_CSV = LOG_PATH + "/history.csv"


class GZipRotator:
    def namer(name):
        return name + ".bz2"

    def rotator(source, dest):
        with open(source, "rb") as fs:
            with bz2.open(dest, "wb") as fd:
                fd.writelines(fs)
        os.remove(source)


def logger_init():
    coloredlogs.install(fmt=LOG_FORMAT)

    log_path = pathlib.Path(os.path.dirname(__file__), LOG_PATH)
    os.makedirs(str(log_path), exist_ok=True)

    logger = logging.getLogger()
    log_handler = logging.handlers.RotatingFileHandler(
        str(log_path / "ui_store_checker.log"),
        encoding="utf8",
        maxBytes=1 * 1024 * 1024,
        backupCount=10,
    )
    log_handler.formatter = logging.Formatter(
        fmt=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"
    )
    log_handler.namer = GZipRotator.namer
    log_handler.rotator = GZipRotator.rotator

    logger.addHandler(log_handler)


def get_abs_path(path):
    return str(pathlib.Path(os.path.dirname(__file__), path))


def load_config():
    config = {}

    with open(get_abs_path(CONFIG_LOGIN_PATH)) as file:
        config.update(yaml.safe_load(file))

    with open(get_abs_path(CONFIG_TARGET_PATH)) as file:
        config["target"] = yaml.safe_load(file)

    return config


def get_memory_info(driver):
    total = subprocess.Popen(
        "smem -t -c pss -P chrome | tail -n 1", shell=True, stdout=subprocess.PIPE
    ).communicate()[0]
    total = int(str(total, "utf-8").strip()) // 1024

    js_heap = driver.execute_script(
        "return window.performance.memory.usedJSHeapSize"
    ) // (1024 * 1024)

    return {"total": total, "js_heap": js_heap}


def dump_page(driver, index):
    name = inspect.stack()[1].function.replace("<", "").replace(">", "")
    dump_path = pathlib.Path(os.path.dirname(__file__), DUMP_PATH)

    os.makedirs(str(dump_path), exist_ok=True)

    png_path = dump_path / (
        "{name}_{index:02d}.{ext}".format(name=name, index=index, ext="png")
    )
    htm_path = dump_path / (
        "{name}_{index:02d}.{ext}".format(name=name, index=index, ext="htm")
    )

    driver.save_screenshot(str(png_path))

    with open(str(htm_path), "w") as f:
        f.write(driver.page_source)


def do_login(driver, wait, config):
    driver.get(LOGIN_URL)

    wait.until(EC.presence_of_element_located((By.XPATH, "//body")))

    if "Account" in driver.title:
        return

    driver.find_element(By.XPATH, '//input[@name="username"]').send_keys(
        config["login"]["user"]
    )
    driver.find_element(By.XPATH, '//input[@name="password"]').send_keys(
        config["login"]["pass"]
    )
    driver.find_element(By.XPATH, '//button[@type="submit"]').click()

    wait.until(EC.presence_of_element_located((By.XPATH, '//input[@type="tel"]')))

    digit_code = input("Authentication App Code: ")
    for i, code in enumerate(list(digit_code)):
        driver.find_element(By.XPATH, '//input[@data-id="' + str(i) + '"]').send_keys(
            code
        )

    wait.until(EC.title_contains("Ubiquiti Account"))


def create_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")  # for Docker
    options.add_argument("--disable-dev-shm-usage")  # for Docker

    options.add_argument("--lang=ja-JP")
    options.add_argument("--window-size=1920,1080")

    options.add_argument("--user-data-dir=" + get_abs_path(CHROME_DATA_PATH))

    # NOTE: 下記がないと，snap で入れた chromium が「LC_ALL: cannot change locale (ja_JP.UTF-8)」
    # と出力し，その結果 ChromeDriverManager がバージョンを正しく取得できなくなる
    os.environ["LC_ALL"] = "C"

    if shutil.which("google-chrome") is not None:
        chrome_type = ChromeType.GOOGLE
    else:
        chrome_type = ChromeType.CHROMIUM

    driver = webdriver.Chrome(
        service=Service(
            ChromeDriverManager(chrome_type=chrome_type).install(),
            log_path="log/webdriver.log",
            service_args=["--verbose"],
        ),
        options=options,
    )

    return driver


def do_stock_check(driver, wait, item):
    driver.get(item["url"])
    wait.until(EC.presence_of_element_located((By.XPATH, "//body")))

    is_in_stock = (
        len(driver.find_elements(By.XPATH, '//span[@id="titleInStockBadge"]')) != 0
    )
    if is_in_stock:
        logging.info("{name} is in stock!".format(name=item["name"]))
    else:
        logging.warning("{name} is out of stock...".format(name=item["name"]))

    return is_in_stock


def write_histstory(in_stock_now, in_stock_before):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9), "JST"))
    date_time = [now.strftime("%Y/%m/%d/"), now.strftime("%H:%M")]
    with open(get_abs_path(HISTORY_CSV), "a") as f:
        writer = csv.writer(f)
        for item_name in in_stock_now:
            if (item_name in in_stock_before) and (
                in_stock_before[item_name] != in_stock_now[item_name]
            ):
                writer.writerow(
                    [
                        date_time[0],
                        date_time[1],
                        item_name,
                        "OK" if in_stock_now[item_name] else "NG",
                    ]
                )


def notify(config, in_stock_now):
    notifier.send(
        config,
        "Inventory status has changed.<br />\n{item_list}".format(
            item_list="<br />\n".join(
                map(
                    lambda item: "- {name}: {status}".format(
                        name=item["name"],
                        status="<b>OK</b>" if in_stock_now[item["name"]] else "NG",
                    ),
                    config["target"],
                )
            )
        ),
    )


os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))

logger_init()

logging.info("開始します．")


driver = create_driver()
wait = WebDriverWait(driver, 5)

in_stock_now = {}
try:
    while True:
        in_stock_before = in_stock_now
        in_stock_now = {}

        config = load_config()
        do_login(driver, wait, config)

        for item in config["target"]:
            in_stock_now[item["name"]] = do_stock_check(driver, wait, item)
            time.sleep(5)

        if (len(in_stock_before) != 0) and (in_stock_now != in_stock_before):
            write_histstory(in_stock_now, in_stock_before)
            notify(config, in_stock_now)

        mem_info = get_memory_info(driver)
        logging.info(
            "Chrome memory: {memory_total:,} MB (JS: {memory_js_heap:,} MB)".format(
                memory_total=mem_info["total"], memory_js_heap=mem_info["js_heap"]
            )
        )
        time.sleep(5 * 60)  # sleep 5min
except:
    logging.error("URL: {url}".format(url=driver.current_url))
    logging.error(traceback.format_exc())
    dump_page(driver, int(random.random() * 100))

driver.close()
driver.quit()

logging.info("完了しました．")
