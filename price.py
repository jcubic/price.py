#!/usr/bin/env python

import smtplib
import sqlite3
import os.path
import os
import re
import logging
import traceback
import sys
from urllib.request import urlopen
from bs4 import BeautifulSoup
from datetime import datetime
from optparse import OptionParser
import smtplib

def init_db():
    DB_NAME = 'price.db'
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for query in open('init.sql').read().split(';'):
        c.execute(query)
    conn.commit()
    return conn

def error(e, host = None, email = None, username = None, password = None):
    logger.error(e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    stack = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(stack)
    try:
        if host is not None:
            message = """From: Me <%s>
To: Me <%s>
Subject: Price Error

There is error in price.py

%s
"""
            s = smtplib.SMTP(host)
            s.login(username, password)
            s.sendmail(email, email, message % (email, email, stack))
            s.quit()
    except Exception as e:
        error(e)

def real_price(str):
    return float(str.replace(',', '.').replace('zł', ''))

def real_score(str):
    return float(re.sub(r'[^0-9,/]|/\s*5', '', str).replace(',', '.'))

def now():
    return int(datetime.now().timestamp())

def int_opinions(str):
    return int(re.sub('[^0-9]', '', str))

def real_delivery(str):
    str = re.sub('[^0-9,]', '', str)
    if len(str) == 0:
        return 0
    return float(str.replace(',', '.'))

def parse(html):
    result = []
    soup = BeautifulSoup(html, 'html.parser')
    items = soup.find_all('li', class_ = "js_productOfferGroupItem")
    if len(items) == 0:
        raise Exception("Error: No entires")
    for item in items:
        entry = {}
        node = item.find('span', class_="price-format")
        if node is None:
            raise Exception("Error: Wrong price html node")
        entry['price'] = real_price(node.text.strip())
        node = item.find(class_ = "stars")
        entry['score'] = real_score(node.text.strip())
        node = item.find(class_ = 'link--accent')
        entry['opinions'] = int_opinions(node.text.strip())
        node = item.find('a', class_ = 'store-logo')
        node = node.find('img')
        if node is None:
            raise Exception("Error: Image with shop log is None")
        entry['shop'] = node['alt']
        if entry['shop'] is None:
            raise Exception("Error: no alt on shop image")
        node = item.find(class_ = 'product-delivery-info')
        delivery = real_delivery(node.text.strip())
        if delivery > 0:
            entry['delivery'] = delivery - entry['price']
        else:
            entry['delivery'] = 0
        node = item.find(class_ = 'product-availability')
        entry['available'] = node.text.strip()
        result.append(entry)
    return result

def create_logger():
    logger = logging.getLogger('price_history')
    fh = logging.FileHandler('error.log')
    ch = logging.StreamHandler()
    ch.setLevel(logging.ERROR)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    fh.setLevel(logging.ERROR)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

def request(url):
    req = urlopen(url)
    code = req.getcode()
    if code == 200:
        return req.read().decode('utf-8')
    else:
        raise Exception("Error Code: %s when accessing %s" % (code, url))

def find(lst, cond):
  for e in lst:
      if cond(e):
          return e
  return None

def get_shop(shops, name):
    return find(shops, lambda x: x[1] == name)

def chwd():
    script = os.path.realpath(__file__)
    path = os.path.dirname(script)
    os.chdir(path)

if __name__ == '__main__':
    parser = OptionParser(usage="%prog [-u|--url] [-p|--product]")
    parser.add_option("-u", "--url", dest="url", action="store",
                      help="url to Ceneo.pl product", metavar="URL")
    parser.add_option("-p", "--product", action="store",
                      dest="product", default = '', metavar="NAME",
                      help="name of the product")
    parser.add_option("", "--username", action="store",
                      dest="username", default = None, metavar="USER",
                      help="SMTP account username")
    parser.add_option("-e", "--email", action="store",
                      dest="email", default = None, metavar="EMAIL",
                      help="email address")
    parser.add_option("", "--host", action="store",
                      dest="host", default = None, metavar="HOST",
                      help="SMPT server hostname")
    parser.add_option("", "--password", action="store",
                      dest="passwd", default = '', metavar="PASSWD",
                      help="SMPT accout password")

    (options, args) = parser.parse_args()
    if options.product is None or options.url is None:
        parser.print_help()
        sys.exit()
    product = options.product
    url = options.url
    try:
        chwd()
        logger = create_logger()
        conn = init_db()
        c = conn.cursor()
        time = now()
        c.execute('INSERT INTO time(time) VALUES (?)', (time,))
        conn.commit()
        time_id = c.lastrowid
        c.execute('SELECT id, name FROM shop')
        shops = c.fetchall()
        c.execute('SELECT id FROM product WHERE name like ?', (product,))
        products = c.fetchall()

        if len(products) == 0:
            c.execute('INSERT INTO product(name) VALUES(?)', (product, ))
            conn.commit()
            product_id = c.lastrowid
        else:
            product_id = products[0][0]

        for offer in parse(request(url)):
            shop = get_shop(shops, offer['shop'])
            if shop is None:
                c.execute('insert INTO shop(name) VALUES(?)', (offer['shop'],))
                conn.commit()
                shop_id = c.lastrowid
            else:
                shop_id = shop[0]
            data = (
                shop_id, product_id, offer['score'], offer['opinions'],
                offer['available'], offer['price'], offer['delivery'],
                time_id
            )
            c.execute('''INSERT INTO price (shop, product, score, opinions, avaiable, price, delivery, time)
                         VALUES(?,?,?,?,?,?,?,?)''', data)

            print("price: %s from %s (%s)" % (offer['price'], offer['shop'], shop_id))
        conn.commit()
    except Exception as e:
        error(
            e,
            host = options.host,
            email = options.email,
            username = options.username,
            password = options.passwd
        )


