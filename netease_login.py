#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云音乐扫码登录（独立脚本，与 QQ 音乐无关）

用法：
  python netease_login.py            未登录则扫码登录，已登录则显示账号信息
  python netease_login.py --force    忽略已有登录态，重新扫码
  python netease_login.py --logout   清空本地登录态

流程：申请 unikey -> 生成二维码（保存为 netease_qr.png 并自动打开）
-> 轮询扫码状态 -> 拿到 MUSIC_U 写进 netease_session.json -> 校验账号信息。

登录态只写在同目录的 netease_session.json，等于你账号的钥匙，请勿外传。
本脚本走的是网页端公开接口，官方随时可能改动。
"""
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(ROOT, 'netease_session.json')
QR_PNG = os.path.join(ROOT, 'netease_qr.png')
QR_LIB = os.path.join(ROOT, '_qrlib')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
APPVER = '8.9.70'
QR_HOSTS = ('https://music.163.com', 'https://interface.music.163.com')
QR_CODES = (800, 801, 802, 803, 8821)
POLL_INTERVAL = 2
POLL_MISS_MAX = 15
QR_REFRESH_MAX = 3

_state = {}


def load():
    global _state
    if _state.get('_loaded'):
        return _state
    data = {}
    try:
        with open(SESSION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get('cookies'), dict):
        data['cookies'] = {}
    data['_loaded'] = True
    _state = data
    return _state


def save():
    st = load()
    out = {k: v for k, v in st.items() if not k.startswith('_')}
    with open(SESSION_FILE, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def cookies():
    return load().get('cookies') or {}


def cookie_header():
    jar = dict(cookies())
    jar.setdefault('os', 'pc')
    jar.setdefault('appver', APPVER)
    return '; '.join('%s=%s' % (k, v) for k, v in jar.items())


def merge(raw_list):
    jar = load()['cookies']
    changed = False
    for raw in raw_list or []:
        kv = str(raw).split(';')[0]
        if '=' not in kv:
            continue
        k, v = kv.split('=', 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        if jar.get(k) != v:
            jar[k] = v
            changed = True
    if changed:
        save()
    return changed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


_OPENER = urllib.request.build_opener(
    _NoRedirect,
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))


def http(url, data=None, method='GET', headers=None, timeout=20):
    hdrs = {'User-Agent': UA, 'Referer': 'https://music.163.com/', 'Accept': '*/*'}
    ck = cookie_header()
    if ck:
        hdrs['Cookie'] = ck
    if headers:
        hdrs.update(headers)
    hops = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            resp = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            raise RuntimeError('请求网易云失败：%s' % e)
        sets = []
        try:
            sets = resp.headers.get_all('Set-Cookie') or []
        except Exception:
            pass
        merge(sets)
        status = getattr(resp, 'status', None) or getattr(resp, 'code', 0)
        loc = resp.headers.get('Location') if resp.headers else None
        if status in (301, 302, 303, 307, 308) and loc and hops < 4:
            hops += 1
            url = urllib.parse.urljoin(url, loc)
            try:
                resp.close()
            except Exception:
                pass
            data, method = None, 'GET'
            ck = cookie_header()
            if ck:
                hdrs['Cookie'] = ck
            else:
                hdrs.pop('Cookie', None)
            continue
        body = b''
        try:
            body = resp.read()
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
        return status, body


def json_body(body):
    text = body.decode('utf-8', 'ignore').strip()
    if text.startswith('callback('):
        text = text[len('callback('):].rstrip(');').strip()
    return json.loads(text)


def profile():
    try:
        _, body = http('https://music.163.com/api/nuser/account/get')
        j = json_body(body)
    except Exception:
        return {'loggedIn': False}
    prof = j.get('profile') or {}
    uid = str(prof.get('userId') or '')
    if not uid:
        return {'loggedIn': False}
    return {'loggedIn': True, 'nick': prof.get('nickname') or '', 'uin': uid,
            'vip': int(prof.get('vipType') or 0) > 0}


def show_profile(prof):
    if prof.get('loggedIn'):
        print('当前已登录：%s（UID %s%s）' % (prof['nick'], prof['uin'],
                                          '，VIP' if prof['vip'] else ''))
    else:
        print('当前未登录。')


def render_qr(text):
    if not os.path.isdir(QR_LIB):
        raise RuntimeError('找不到二维码库目录 %s' % QR_LIB)
    if QR_LIB not in sys.path:
        sys.path.insert(0, QR_LIB)
    import qrcode
    from qrcode.image.pure import PyPNGImage
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    qr.make_image(image_factory=PyPNGImage).save(QR_PNG)
    return qr


def open_qr(path):
    try:
        if hasattr(os, 'startfile'):
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass


def warmup():
    jar = cookies()
    for k in ('NMTID', '__csrf', 'JSESSIONID-WYYY'):
        jar.pop(k, None)
    save()
    for url in ('https://music.163.com/',
                'https://music.163.com/api/song/enhance/player/url'
                '?ids=%5B1%5D&br=999000&os=pc&appver=' + APPVER):
        try:
            http(url, timeout=15)
        except Exception:
            pass


def create_key():
    _, body = http('https://music.163.com/api/login/qrcode/unikey?os=pc&appver=' + APPVER,
                   data=b'type=1', method='POST',
                   headers={'Content-Type': 'application/x-www-form-urlencoded'})
    j = json_body(body)
    key = str(j.get('unikey') or '')
    if not key:
        raise RuntimeError('二维码获取失败：%s'
                           % body.decode('utf-8', 'ignore')[:120])
    return key


def poll(key):
    for host in QR_HOSTS:
        for method in ('POST', 'GET'):
            try:
                if method == 'POST':
                    _, body = http(host + '/api/login/qrcode/client/login?os=pc&appver=' + APPVER,
                                   data=('key=%s&type=1' % key).encode('utf-8'),
                                   method='POST',
                                   headers={'Content-Type': 'application/x-www-form-urlencoded'})
                else:
                    _, body = http(host + '/api/login/qrcode/client/login'
                                   '?os=pc&appver=%s&type=1&key=%s'
                                   % (APPVER, urllib.parse.quote(key)))
                j = json_body(body)
                code = int(j.get('code') or 0)
                if code in QR_CODES:
                    return code, j
            except Exception:
                continue
    return 0, {}


def finish(j):
    ck = str(j.get('cookie') or '')
    if ck:
        merge(re.split(r'[;,]', ck))
    if not cookies().get('MUSIC_U'):
        print('扫码已确认，但网易云没有返回登录凭证（MUSIC_U），请重新扫码。')
        return False
    st = load()
    st['loginTime'] = int(time.time())
    save()
    prof = {'loggedIn': False}
    for _ in range(3):
        prof = profile()
        if prof.get('loggedIn'):
            break
        time.sleep(0.4)
    if prof.get('loggedIn'):
        st['uin'] = prof.get('uin') or ''
        save()
        print('登录成功：%s（UID %s%s）' % (prof['nick'], prof['uin'],
                                         '，VIP' if prof['vip'] else ''))
    else:
        print('登录凭证已保存，但账号信息校验失败，请稍后重试。')
    return bool(prof.get('loggedIn'))


def login():
    warmup()
    key = ''
    refreshes = 0
    scanned = False
    misses = 0
    while True:
        if not key:
            key = create_key()
            url = 'https://music.163.com/login?codekey=' + key
            try:
                qr = render_qr(url)
            except Exception as e:
                print('二维码生成失败：%s' % e)
                print('请手动打开链接：%s' % url)
                qr = None
            else:
                open_qr(QR_PNG)
                try:
                    qr.print_ascii(invert=True)
                except Exception:
                    pass
            print('请用网易云音乐 App 扫码登录（二维码已保存到 %s）' % QR_PNG)
            print('二维码链接：%s' % url)
        time.sleep(POLL_INTERVAL)
        code, j = poll(key)
        if code == 803:
            return finish(j)
        if code == 8821:
            print('网易云风控拦截：%s'
                  % (j.get('message') or '请换个网络或稍后再试'))
            return False
        if code == 802 and not scanned:
            scanned = True
            print('已扫码，请在手机上确认登录…')
        if code == 800:
            refreshes += 1
            if refreshes >= QR_REFRESH_MAX:
                print('二维码多次过期，已停止。请重新运行本脚本。')
                return False
            print('二维码已过期，正在刷新…')
            key = ''
            scanned = False
            continue
        if code in (801, 802):
            misses = 0
            continue
        misses += 1
        if misses >= POLL_MISS_MAX:
            print('轮询多次失败，正在刷新二维码…')
            key = ''
            scanned = False
            misses = 0


def logout():
    st = load()
    st['cookies'] = {}
    st.pop('uin', None)
    save()
    print('已清空本地登录态。')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    args = [a.lower() for a in sys.argv[1:]]
    if '--logout' in args or '-l' in args:
        logout()
        return 0
    force = '--force' in args or '-f' in args
    if not force:
        prof = profile()
        show_profile(prof)
        if prof.get('loggedIn'):
            print('如需重新扫码，运行：python netease_login.py --force')
            return 0
    return 0 if login() else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已取消。')
        sys.exit(130)