#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云音乐扫码登录（复用小手机页面的二维码）

用法：
  python netease_login.py                    未登录则扫码登录，已登录则显示账号信息
  python netease_login.py --force            忽略已有登录态，重新扫码
  python netease_login.py --logout           清空登录态
  python netease_login.py --base <服务地址>   指定桥接服务地址（默认 http://127.0.0.1:8000）

流程：本脚本不再自己申请二维码，而是直接调用本机 qqmusic_server.py 的
/api/ne/qr 与 /api/ne/qr/check，拿到与 Music.html 页面完全相同的那张二维码，
并自动在浏览器打开「小手机」的网易云登录页（Music.html?login=ne）展示这张码；
扫码确认后由服务写入 netease_session.json，页面立刻同步显示已登录。

使用前请先启动 qqmusic_server.py，否则脚本无法工作。
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
QR_PNG = os.path.join(ROOT, 'netease_qr.png')
QR_LIB = os.path.join(ROOT, '_qrlib')
BASE = (os.environ.get('NE_BASE') or 'http://127.0.0.1:8000').rstrip('/')
POLL_INTERVAL = 2
POLL_MISS_MAX = 15
QR_REFRESH_MAX = 3


def api(path, data=None, method='GET'):
    url = BASE + path
    body = None
    headers = {'Accept': 'application/json'}
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8', 'ignore') or '{}')
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8', 'ignore') or '{}')
        except Exception:
            return {}
    except Exception as e:
        raise RuntimeError('无法连接本机服务 %s（%s）。请先运行：python qqmusic_server.py'
                           % (BASE, e))


def profile():
    try:
        j = api('/api/ne/status')
    except Exception:
        return {'loggedIn': False}
    prof = j.get('profile') or {}
    if not prof.get('loggedIn'):
        return {'loggedIn': False}
    return {'loggedIn': True, 'nick': prof.get('nick') or '', 'uin': prof.get('uin') or '',
            'vip': bool(prof.get('vip'))}


def show_profile(prof):
    if prof.get('loggedIn'):
        print('当前已登录：%s（UID %s%s）' % (prof['nick'], prof['uin'],
                                          '，VIP' if prof['vip'] else ''))
    else:
        print('当前未登录。')


def save_qr_image(image):
    if not image or ',' not in image:
        return ''
    try:
        png = base64.b64decode(image.split(',', 1)[1])
        with open(QR_PNG, 'wb') as f:
            f.write(png)
    except Exception:
        return ''
    return QR_PNG


def qr_key():
    try:
        j = api('/api/ne/diag')
    except Exception:
        return ''
    return str(j.get('key') or '')


def page_url():
    return BASE + '/Music.html?login=ne'


def open_page():
    url = page_url()
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


def show_ascii(key):
    if not key or not os.path.isdir(QR_LIB):
        return
    if QR_LIB not in sys.path:
        sys.path.insert(0, QR_LIB)
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=10, border=4)
        qr.add_data('https://music.163.com/login?codekey=' + key)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def finish(j):
    prof = j.get('profile') or {}
    if j.get('loggedIn') and prof.get('loggedIn'):
        print('登录成功：%s（UID %s%s）' % (prof.get('nick') or '', prof.get('uin') or '',
                                         '，VIP' if prof.get('vip') else ''))
        return True
    print(j.get('error') or '登录凭证已保存，但账号信息校验失败，请稍后重试。')
    return False


def login():
    key = ''
    refreshes = 0
    scanned = False
    misses = 0
    opened = False
    print('正在通过本机服务获取二维码…')
    while True:
        if not key:
            j = api('/api/ne/qr')
            if not j.get('ok') or not j.get('image'):
                print('二维码获取失败：%s' % (j.get('error') or '服务没有返回图片'))
                return False
            key = str(j.get('key') or '') or qr_key()
            saved = save_qr_image(j['image'])
            if saved:
                print('二维码图片已保存到 %s' % saved)
            if not opened:
                opened = True
                if open_page():
                    print('已在浏览器打开网易云登录页：%s' % page_url())
                else:
                    print('请手动打开：%s' % page_url())
            show_ascii(key)
            if key:
                print('二维码链接：https://music.163.com/login?codekey=%s' % key)
            print('请用网易云音乐 App 扫页面上的二维码（页面与终端是同一张码）')
        time.sleep(POLL_INTERVAL)
        j = api('/api/ne/qr/check')
        nk = str(j.get('key') or '')
        if nk and nk != key and j.get('image'):
            key = nk
            save_qr_image(j['image'])
            print('二维码已更新（页面同步）：https://music.163.com/login?codekey=%s' % key)
        status = int(j.get('status') or 0)
        if status == 0:
            return finish(j)
        if status == 69:
            print('网易云风控拦截：%s'
                  % (j.get('error') or '请换个网络或稍后再试'))
            return False
        if status == 67:
            if not scanned:
                scanned = True
                print('已扫码，请在手机上确认登录…')
            misses = 0
            continue
        if status == 66:
            misses = 0
            continue
        if status in (68, 1):
            print('已取消登录，正在刷新二维码…')
            key = ''
            scanned = False
            continue
        if status == 65:
            if j.get('error'):
                print(j['error'])
                return False
            refreshes += 1
            if refreshes >= QR_REFRESH_MAX:
                print('二维码多次过期，已停止。请重新运行本脚本。')
                return False
            print('二维码已过期，正在刷新…')
            key = ''
            scanned = False
            continue
        misses += 1
        if misses >= POLL_MISS_MAX:
            print('轮询多次失败，正在刷新二维码…')
            key = ''
            scanned = False
            misses = 0


def logout():
    api('/api/ne/logout', data={}, method='POST')
    print('已清空登录态（服务和页面同步生效）。')


def main():
    global BASE
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ('--base', '-b') and i + 1 < len(args):
            BASE = args[i + 1].rstrip('/')
    low = [a.lower() for a in args]
    try:
        api('/api/ne/status')
    except RuntimeError as e:
        print(e)
        return 1
    if '--logout' in low or '-l' in low:
        logout()
        return 0
    force = '--force' in low or '-f' in low
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