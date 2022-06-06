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
from selenium.webdriver.support import expected_conditions as EC
import time
import os
import sys
import random

import yaml
import pprint
import pathlib
import traceback

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

    log_path = pathlib.Path(LOG_PATH)
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
    dump_path = pathlib.Path(DUMP_PATH)

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
    options.add_argument("--lang=ja-JP")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        '--user-agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.80 Safari/537.36"'
    )
    options.add_argument("--user-data-dir=" + get_abs_path(CHROME_DATA_PATH))

    import chromedriver_binary

    driver = webdriver.Chrome(options=options)

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
        logging.info("{name} is out of stock...".format(name=item["name"]))

    return is_in_stock


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
            notifier.send(
                config,
                "Inventory status has changed.<br />\n{item_list}".format(
                    item_list="<br />\n".join(
                        map(
                            lambda item: "- {name}: {status}".format(
                                name=item["name"],
                                status="<b>OK</b>"
                                if in_stock_now[item["name"]]
                                else "NG",
                            ),
                            config["target"],
                        )
                    )
                ),
            )

        mem_info = get_memory_info(driver)
        logging.info(
            "Chrome memory: {memory_total:,} MB (JS: {memory_js_heap:,} MB)".format(
                memory_total=mem_info["total"], memory_js_heap=mem_info["js_heap"]
            )
        )
        time.sleep(60 * 60)  # sleep 1 hour
except:
    logging.error("URL: {url}".format(url=driver.current_url))
    logging.error(traceback.format_exc())
    dump_page(driver, int(random.random() * 100))

driver.close()
driver.quit()

logging.info("完了しました．")
