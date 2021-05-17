#!/usr/bin/env python3

import re
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from http.cookies import SimpleCookie
import arrow
import argparse
import os
import random
import sys
import requests
import logging
import json
from pathlib import Path
from urllib import parse

cicd_index_url = os.environ['INDEX_HOST']

FORMAT = '[%(levelname)s] %(name) -12s %(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logging.getLogger().setLevel(logging.DEBUG)
logger = logging.getLogger('')  # root handler
logger.info("Starting cicd delegator reverse-proxy")

def ignore_case_get(dict, key):
    keys = list(dict.keys())
    lkeys = [x.lower() for x in keys]
    idx = lkeys.index(key.lower())
    if idx >= 0:
        return dict[keys[idx]]
    return None

def split_set_cookie(cookie, as_simple_cookie=False):
    """
    roundcube_sessauth=-del-; expires=Tue, 02-Mar-2021 16:36:26 GMT; Max-Age=0; path=/;
    HttpOnly, roundcube_sessid=93gt0c9a8c7njtt5f6tpa0t1h2; path=/; HttpOnly,
    roundcube_sessauth=Od9cAxp8lkWwbsjjQ8KWMNQBRW-1614702900; path=/; HttpOnly'
    """
    orig_cookie = cookie
    while ' =' in cookie:
        cookie = cookie.replace(' =', '=')
    arr = cookie.split(";")
    cookies = []

    keywords = ['expires', 'max-age', 'domain', 'path', 'httponly']

    def extract_keywords(s):
        found = []
        splitted = s.split(',')
        filtered = []
        for x in splitted:
            if x.lower().strip() in keywords and '=' not in x:
                # e.g. HttpOnly, MyCookie=123
                found.append(x)
                x = ""
            else:
                for kw in keywords:
                    if x.lower().startswith(kw + '='):
                        filtered.append(x)
                        x = ""
            if x:
                filtered.append(x)
        return found, ','.join(filtered)

    for part in arr:
        part = part.strip()

        # extract keywords and append
        append, part = extract_keywords(part)

        if '=' in part:
            if not any(part.strip().lower().startswith(x + '=') for x in keywords):
                cookies.append([])

        cookies[-1].append(part.strip())
        if append:
            cookies[-1] += append
            append = []

    cookies = [';'.join(x) for x in cookies]
    if as_simple_cookie:
        cookies = ';\n'.join(cookies)
        cookies = SimpleCookie(cookies)

    print(f"{orig_cookie} -----------> {cookies}")
    return cookies

class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _merge_headers(self, *arrs):
        headers = sum(arrs)
        return headers

    def do_HEAD(self):
        self.do_GET(body=False)

    def _handle_error(self, ex):
        logger.error(ex)
        self.send_error(501, str(ex))

    def _rewrite_path(self, header, cookies):
        url = ""
        if cookies and cookies.get('delegator-path'):
            delegator_path = cookies.get('delegator-path', "")
            delegator_path = delegator_path and delegator_path.value
        else:
            delegator_path = 'not-set'
        if delegator_path == 'not-set':
            delegator_path = ""

        if delegator_path:
            # set touched date:
            requests.get(cicd_index_url + "/last_access", params={'site': delegator_path}).raise_for_status()

        logger.info(f"rewrite path: self.path: {self.path}, delegator_path: {delegator_path}")

        path = (self.path or '').split("?")[0]
        if path in ['/index', '/index/'] or "/__start_cicd" in path or not delegator_path or path.startswith("/cicd/"):
            path = self.path
            if path.split("/")[1] == 'index':
                path = '/'
            else:
                path = '/' + '/'.join(path.split("/")[2:])
            url = f'{cicd_index_url}{path}'
        elif path.startswith("/mailer/") and delegator_path:
            host = f"{delegator_path}_proxy"
            url = f'http://{host}{path}'
        else:
            host = f"{delegator_path}_proxy"
            path_old = path
            if self.path.endswith(f"/{delegator_path}"):
                path = self.path.replace(f"/{delegator_path}", "")
            else:
                path = self.path.replace(f"/{delegator_path}/", "/")
            url = f'http://{host}{path}'

        logger.debug(f"rewrite path result: {url}")
        return url

    def _redirect_to_index(self):
        # do logout to odoo to be clean; but redirect to index
        self.send_response(302)
        self.send_header('Location', '/index')
        self.end_headers()

    def do_GET(self, body=True):
        sent = False

        query_params = dict(parse.parse_qsl(parse.urlsplit(self.path).query))
        try:
            req_header, cookies = self.parse_headers()
            url = self._rewrite_path(req_header, cookies)
            resp = requests.get(
                url, headers=req_header, verify=False,
                allow_redirects=False, params=query_params,
                cookies={k: v.value for k, v in cookies.items()},
            )
            sent = True

            if self.path == '/web/session/logout':
                self._redirect_to_index()
            else:
                self.send_response(resp.status_code)
                self.send_resp_headers(resp, cookies)
                if body:
                    self.wfile.write(resp.content)

            return
        except Exception as ex:
            self._handle_error(ex)
        finally:
            self.finish()
            if not sent:
                self.send_error(404, 'error trying to proxy')

    def do_POST(self, body=True):
        req_header, cookies = self.parse_headers()
        url = self._rewrite_path(req_header, cookies)
        sent = False
        try:
            content_len = int(self.headers.get('content-length', 0))
            post_body = self.rfile.read(content_len)

            resp = requests.post(
                url, data=post_body,  headers=req_header,
                verify=False, allow_redirects=False,
                cookies={k: v.value for k, v in cookies.items()},
            )
            sent = True

            self.send_response(resp.status_code)
            self.send_resp_headers(resp, cookies)
            if body:
                self.wfile.write(resp.content)
            return
        except Exception as ex:
            import traceback
            msg = traceback.format_exc()
            logger.error(msg)
        finally:
            self.finish()
            if not sent:
                self.send_error(404, 'error trying to proxy')

    def parse_headers(self):
        req_header = {}
        for line in self.headers.as_string().split("\n"):
            if not line:
                continue
            line_parts = [o.strip() for o in line.split(':', 1)]
            if len(line_parts) == 2:
                key = line_parts[0]
                if key.lower() == 'cookie':
                    key = 'Cookie'
                req_header[key] = line_parts[1]

        cookies = SimpleCookie()
        if req_header.get('Cookie'):
            cookies = split_set_cookie(req_header['Cookie'], as_simple_cookie=True)

        return req_header, cookies

    def _set_cookies(self, cookie):
        logger.debug(f"Path is: {self.path}")

        if '/__start_cicd' in self.path:
            site = self.path.split("/")[1]
            cookie['delegator-path'] = site
            cookie['delegator-path']['max-age'] = 365 * 24 * 3600
            cookie['delegator-path']['path'] = '/'
        elif self.path in ['/index', '/index/', '/web/session/logout']:
            cookie['delegator-path'] = "not-set"
            cookie['delegator-path']['path'] = '/'

    def send_resp_headers(self, resp, cookies):
        self._set_cookies(cookies)

        respheaders = resp.headers
        logger.debug('Response Header')
        for key in respheaders:
            if (key or '').lower() not in [
                'content-encoding', 'transfer-encoding', 'content-length',
                'set-cookie',
            ]:
                self.send_header(key, respheaders[key])
        self.send_header('Content-Length', len(resp.content))

        if resp.headers.get('set-cookie'):
            for cookie in split_set_cookie(resp.headers.get('set-cookie')):
                self.send_header("Set-Cookie", cookie)

        self.end_headers()


def parse_args(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser(description='Proxy HTTP requests')
    parser.add_argument(
        '--port', dest='port', type=int,
        default=80, help='serve HTTP requests on specified port (default: 80)'
    )
    args = parser.parse_args(argv)
    return args

def main(argv=sys.argv[1:]):
    args = parse_args(argv)
    logger.info('http server is starting on port {}...'.format(args.port))
    server_address = ('0.0.0.0', args.port)
    httpd = ThreadingHTTPServer(server_address, ProxyHTTPRequestHandler)
    logger.info('http server is running as reverse proxy')
    logger.info(f"Starting reverse proxy on {server_address}")
    httpd.serve_forever()


if __name__ == '__main__':
    main()
