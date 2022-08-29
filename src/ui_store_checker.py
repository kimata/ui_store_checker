#!/usr/bin/env python3
# - coding: utf-8 --
import logging
import logging.handlers
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

import pathlib
import datetime
import traceback
import csv
import pickle

import logger
import notifier
from config import load_config

CONFIG_TARGET_PATH = "../target.yaml"

LOGIN_URL = "https://sso.ui.com/api/sso/v1/shopify_login"


DATA_PATH = pathlib.Path(os.path.dirname(__file__)).parent / "data"
LOG_PATH = DATA_PATH / "log"

CHROME_DATA_PATH = str(DATA_PATH / "chrome")
DUMP_PATH = str(DATA_PATH / "deubg")

DRIVER_LOG_PATH = str(LOG_PATH / "webdriver.log")
HIST_CSV_PATH = str(LOG_PATH / "history.csv")
STOCK_CACHE_PATH = str(LOG_PATH / "stock_cache.dat")


def serialize_load(path, init={}):
    if pathlib.Path(path).exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    else:
        return init


def serialize_store(path, cache):
    with open(path, "wb") as f:
        pickle.dump(cache, f)


def stock_cache_load():
    return serialize_load(STOCK_CACHE_PATH, {})


def stock_cache_store(stock_cache):
    return serialize_store(STOCK_CACHE_PATH, stock_cache)


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

    options.add_argument("--user-data-dir=" + CHROME_DATA_PATH)

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
            log_path=DRIVER_LOG_PATH,
            service_args=["--verbose"],
        ),
        options=options,
    )

    return driver


def do_stock_check(driver, wait, item, before_stock):
    driver.get(item["url"])
    wait.until(EC.presence_of_element_located((By.XPATH, "//body")))

    is_in_stock = (
        len(driver.find_elements(By.XPATH, '//span[@id="titleInStockBadge"]')) != 0
    )
    logging.info("check {name}".format(name=item["name"]))
    if is_in_stock != before_stock:
        if is_in_stock:
            logging.info("{name} is in stock!".format(name=item["name"]))
        else:
            logging.info("{name} is out of stock...".format(name=item["name"]))

    return is_in_stock


def write_histstory(in_stock_now, in_stock_before):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9), "JST"))
    date_time = [now.strftime("%Y/%m/%d/"), now.strftime("%H:%M")]
    with open(HIST_CSV_PATH, "a") as f:
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

logger.init("bot.ui_store.checker")

driver = create_driver()
wait = WebDriverWait(driver, 5)

in_stock_now = stock_cache_load()
try:
    while True:
        logging.info("Start.")
        in_stock_before = in_stock_now
        in_stock_now = {}

        config = load_config()
        config["target"] = load_config(CONFIG_TARGET_PATH)
        do_login(driver, wait, config)

        for item in config["target"]:
            in_stock_now[item["name"]] = do_stock_check(
                driver,
                wait,
                item,
                in_stock_before[item["name"]]
                if item["name"] in in_stock_before
                else False,
            )
            time.sleep(5)

        if (len(in_stock_before) != 0) and (in_stock_now != in_stock_before):
            write_histstory(in_stock_now, in_stock_before)
            notify(config, in_stock_now)

        stock_cache_store(in_stock_now)

        logging.info("Finish.")
        pathlib.Path(config["liveness"]["file"]).touch()

        mem_info = get_memory_info(driver)
        logging.info(
            "Chrome memory: {memory_total:,} MB (JS: {memory_js_heap:,} MB)".format(
                memory_total=mem_info["total"], memory_js_heap=mem_info["js_heap"]
            )
        )

        sleep_time = config["check"]["interval"] - datetime.datetime.now().second
        logging.info("sleep {sleep_time} sec...".format(sleep_time=sleep_time))
        time.sleep(sleep_time)
except:
    logging.error("URL: {url}".format(url=driver.current_url))
    logging.error(traceback.format_exc())
    dump_page(driver, int(random.random() * 100))

driver.close()
driver.quit()

sys.exit(-1)
