#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 音乐本机桥接服务（仅在本机运行）

为小手机（index.html / Music.html）提供三件事：
  1. QQ 音乐扫码登录：二维码、轮询、登录态 cookie
  2. 曲库读取：自建歌单、歌单内歌曲（含 VIP 曲目）
  3. 音频流：用你的账号换取播放地址

登录态只写在同目录的 qqmusic_session.json 里，等于你账号的钥匙，请勿外传。
QQ 音乐没有开放平台接口，本服务走的是网页端公开接口，官方随时可能改动。

启动：python qqmusic_server.py
打开：http://localhost:8000/index.html
"""
import base64
import json
import os
import random
import re
import ssl
import struct
import sys
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(ROOT, 'qqmusic_session.json')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
APPID = '716027609'
PT_3RD_AID = '100497308'
U1 = 'https://graph.qq.com/oauth2.0/login_jump'
WX_APPID = 'wxae3d083b10ae7844'
WX_REDIRECT = 'https://y.qq.com/portal/wx_redirect.html'
SSL_CTX = ssl._create_unverified_context()

_state = {}
_qr = {'sig': '', 'time': 0}
_wxqr = {'uuid': '', 'time': 0, 'image': '', 'code': ''}
_trace = []


def load_state():
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


def save_state():
    st = load_state()
    out = {k: v for k, v in st.items() if not k.startswith('_')}
    try:
        with open(SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cookies():
    return load_state().get('cookies') or {}


def cookie_header():
    return '; '.join('%s=%s' % (k, v) for k, v in cookies().items())


def merge_cookies(raw_list):
    st = load_state()
    jar = st['cookies']
    changed = False
    for raw in raw_list or []:
        kv = str(raw).split(';')[0]
        if '=' not in kv:
            continue
        k, v = kv.split('=', 1)
        k, v = k.strip(), v.strip()
        if k and v and jar.get(k) != v:
            jar[k] = v
            changed = True
    if changed:
        save_state()
    return changed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(url, headers=None, data=None, method='GET', follow=True,
         timeout=20, use_cookies=True, raw_headers=False):
    hdrs = {'User-Agent': UA, 'Accept': '*/*'}
    if use_cookies and cookie_header():
        hdrs['Cookie'] = cookie_header()
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    hops = 0
    while True:
        try:
            resp = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            raise RuntimeError('请求失败：%s' % e)
        sets = []
        try:
            sets = resp.headers.get_all('Set-Cookie') or []
        except Exception:
            pass
        if use_cookies:
            merge_cookies(sets)
        status = getattr(resp, 'status', None) or getattr(resp, 'code', 0)
        loc = resp.headers.get('Location') if resp.headers else None
        if follow and status in (301, 302, 303, 307, 308) and loc and hops < 6:
            hops += 1
            url = urllib.parse.urljoin(url, loc)
            try:
                resp.close()
            except Exception:
                pass
            if use_cookies:
                if cookie_header():
                    hdrs['Cookie'] = cookie_header()
                else:
                    hdrs.pop('Cookie', None)
            req = urllib.request.Request(url, headers=hdrs, method='GET')
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
        return status, resp.headers, body, url


def _text(body):
    return body.decode('utf-8', 'ignore')


def _json_body(body):
    t = _text(body).strip()
    if t.startswith('callback('):
        t = t[len('callback('):].rstrip(');').strip()
    return json.loads(t)


def hash33(text):
    e = 0
    for ch in text:
        e += (e << 5) + ord(ch)
        e &= 0x7fffffff
    return e


UIN_KEYS = ('uin', 'p_uin', 'ptui_loginuin', 'pt2gguin', 'luin', 'lskey_uin', 'qqmusic_uin')
KEY_KEYS = ('qm_keyst', 'qqmusic_key', 'qqmusic_key2', 'musickey')


def _clean_uin(v):
    s = str(v or '').strip()
    if s[:1] in ('o', 'O'):
        s = s[1:]
    if s[:1] == '0' and len(s) > 1:
        s = s[1:]
    return s


def _uin():
    st = load_state()
    ck = cookies()
    for k in UIN_KEYS:
        v = ck.get(k)
        if v:
            return _clean_uin(v)
    return _clean_uin(st.get('uin'))


def _dbg(msg):
    try:
        with open(os.path.join(ROOT, 'qqmusic_login.log'), 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%H:%M:%S'), msg))
    except Exception:
        pass
    _trace.append(str(msg))
    if len(_trace) > 60:
        del _trace[0:len(_trace) - 60]


def _dump(tag, status, headers, body):
    loc = ''
    try:
        loc = headers.get('Location') or ''
    except Exception:
        pass
    names = []
    try:
        for raw in (headers.get_all('Set-Cookie') or []):
            names.append(str(raw).split('=')[0].strip())
    except Exception:
        pass
    txt = _text(body or b'')
    head = txt.strip().replace('\n', ' ')[:120]
    _dbg('%s -> %s loc=%s set=%s body=%s' % (tag, status, loc[:90], '|'.join(names), head))


def _music_key():
    ck = cookies()
    for k in KEY_KEYS:
        if ck.get(k):
            return str(ck[k])
    return ''


def _ensure_music_key():
    if _music_key():
        return True
    for u in ('https://y.qq.com/', 'https://y.qq.com/portal/profile.html',
              'https://y.qq.com/n/ryqq/profile'):
        try:
            st, hd, body, _ = http(u, headers={'Referer': 'https://y.qq.com/'}, follow=True)
            _dump('ensure %s' % u.split('/')[2] + '/' + '/'.join(u.split('/')[3:])[:20], st, hd, b'')
        except Exception as e:
            _dbg('ensure %s 失败 %s' % (u, e))
        if _music_key():
            return True
    return False


def qq_qr_create():
    now = time.time()
    if _qr.get('sig') and _qr.get('image') and now - (_qr.get('time') or 0) < 90:
        return {'ok': True, 'image': _qr['image'], 'reused': True}
    qs = ('appid=%s&e=2&l=M&s=3&d=72&v=4&t=%.4f&daid=383&pt_3rd_aid=%s&u1=%s'
          % (APPID, now, PT_3RD_AID, urllib.parse.quote(U1, safe='')))
    body = b''
    host = 'ssl.ptlogin2.qq.com'
    for h in ('ssl.ptlogin2.graph.qq.com', 'ssl.ptlogin2.qq.com'):
        try:
            st, hd, b, _ = http('https://%s/ptqrshow?%s' % (h, qs),
                                headers={'Referer': 'https://graph.qq.com/'})
        except Exception as e:
            _dbg('qrshow %s 失败 %s' % (h, e))
            continue
        if b and b[:4] == b'\x89PNG':
            body, host = b, h
            break
    sig = cookies().get('qrsig', '')
    if not sig or not body:
        raise RuntimeError('二维码获取失败')
    image = 'data:image/png;base64,' + base64.b64encode(body).decode()
    _qr['sig'] = sig
    _qr['time'] = now
    _qr['image'] = image
    _qr['host'] = host
    return {'ok': True, 'image': image}


def _qr_poll(host, sig, token):
    url = ('https://%s/ptqrlogin?u1=%s&ptqrtoken=%d&ptredirect=0&h=1&t=1&g=1'
           '&from_ui=1&ptlang=2052&action=0-0-%.4f&js_ver=20102616&js_type=1&login_sig='
           '&pt_uistyle=40&aid=%s&daid=383&pt_3rd_aid=%s&has_onekey=1'
           % (host, urllib.parse.quote(U1, safe=''), token, time.time(), APPID, PT_3RD_AID))
    st, hd, body, _ = http(url, use_cookies=False,
                           headers={'Cookie': 'qrsig=' + sig,
                                    'Referer': 'https://xui.ptlogin2.qq.com/'})
    txt = _text(body)
    return re.search(r"(?:ptuiCB|ptqrlogin)\(\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'",
                     txt), txt


def qq_qr_check():
    sig = _qr.get('sig')
    if not sig:
        return {'ok': False, 'status': 65, 'msg': '二维码已失效', 'loggedIn': False}
    token = hash33(sig)
    host = _qr.get('host') or 'ssl.ptlogin2.qq.com'
    m, txt = _qr_poll(host, sig, token)
    if not m:
        other = 'ssl.ptlogin2.qq.com' if host != 'ssl.ptlogin2.qq.com' else 'ssl.ptlogin2.graph.qq.com'
        m2, txt2 = _qr_poll(other, sig, token)
        if m2:
            m, txt = m2, txt2
    if not m:
        _dbg('qr_check 无匹配 body=%s' % txt[:150].replace('\n', ' '))
        return {'ok': True, 'status': 66, 'msg': '等待扫码', 'loggedIn': False}
    code = m.group(1)
    jump = m.group(3)
    msg = m.group(5) or ''
    if code != '66':
        _dbg('qr_check code=%s msg=%s jump=%s' % (code, msg, jump[:120]))
    if code == '0':
        if not jump.startswith('http'):
            jump = m.group(2)
        return _finish_login(jump, txt)
    if code not in ('66', '67'):
        _qr['sig'] = ''
    return {'ok': True, 'status': int(code), 'msg': msg, 'loggedIn': False}


def _wx_exchange(code):
    payload = {
        'comm': {'tmeAppID': 'qqmusic', 'ct': 24, 'cv': 0, 'v': '10000', 'uin': ''},
        'req': {'module': 'music.login.LoginServer', 'method': 'Login',
                'param': {'code': code, 'musicuniq': 1, 'sesscell': '', 'partlen': 0}}
    }
    st, hd, body, _ = http('https://u.y.qq.com/cgi-bin/musicu.fcg',
                           data=json.dumps(payload).encode('utf-8'), method='POST',
                           use_cookies=False,
                           headers={'Referer': 'https://y.qq.com/',
                                    'Content-Type': 'application/json'})
    j = _json_body(body)
    req = j.get('req') or {}
    _dbg('wx exchange code=%s' % (req.get('code'),))
    return int(req.get('code') or 0), (req.get('data') or {})


def _wx_poll(uuid, last=''):
    u = ('https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid=%s&_=%d'
         % (uuid, int(time.time() * 1000)))
    if last:
        u += '&last=' + last
    try:
        st, hd, body, _ = http(u, use_cookies=False, timeout=15,
                               headers={'Referer': 'https://open.weixin.qq.com/'})
    except Exception as e:
        _dbg('wx poll 失败 %s' % e)
        return '', ''
    t = _text(body)
    mc = re.search(r"wx_code='([^']*)'", t)
    me = re.search(r'wx_errcode=(\d+)', t)
    return (mc.group(1) if mc else ''), (me.group(1) if me else '')


def wx_qr_create():
    now = time.time()
    if _wxqr.get('uuid') and _wxqr.get('image') and now - (_wxqr.get('time') or 0) < 170:
        return {'ok': True, 'image': _wxqr['image'], 'reused': True}
    url = ('https://open.weixin.qq.com/connect/qrconnect?appid=%s&redirect_uri=%s'
           '&response_type=code&scope=snsapi_login&state=%s&self_redirect=true&login_type=jssdk'
           % (WX_APPID, urllib.parse.quote(WX_REDIRECT, safe=''), str(int(now * 1000))))
    st, hd, body, final = http(url, use_cookies=False, headers={'Referer': 'https://y.qq.com/'})
    t = _text(body)
    m1 = re.search(r'/connect/qrcode/([0-9A-Za-z_\-]{10,})', t)
    if not m1:
        raise RuntimeError('微信二维码获取失败（appid 或回调地址被拒）')
    uuid = m1.group(1)
    st2, hd2, img, _ = http('https://open.weixin.qq.com/connect/qrcode/' + uuid,
                            use_cookies=False, timeout=15,
                            headers={'Referer': 'https://open.weixin.qq.com/'})
    if not img:
        raise RuntimeError('微信二维码图片下载失败')
    ctype = 'jpeg'
    if img[:4] == b'\x89PNG':
        ctype = 'png'
    image = 'data:image/%s;base64,%s' % (ctype, base64.b64encode(img).decode())
    _wxqr.update({'uuid': uuid, 'time': now, 'image': image, 'code': ''})
    return {'ok': True, 'image': image}


def wx_qr_check():
    uuid = _wxqr.get('uuid')
    if not uuid:
        return {'ok': False, 'status': 65, 'msg': '二维码已失效', 'loggedIn': False}
    code = _wxqr.get('code') or ''
    if not code:
        code, errcode = _wx_poll(uuid)
        if not code and errcode == '405':
            code, errcode = _wx_poll(uuid, last='405')
        if not code:
            if errcode == '404':
                return {'ok': True, 'status': 67, 'msg': '已扫码，请在手机上确认',
                        'loggedIn': False}
            if errcode in ('402', '403'):
                _wxqr.update({'uuid': '', 'image': '', 'code': ''})
                return {'ok': True, 'status': 65, 'msg': '二维码已失效', 'loggedIn': False}
            return {'ok': True, 'status': 66, 'msg': '等待扫码', 'loggedIn': False}
    _wxqr['code'] = code
    try:
        rc, d = _wx_exchange(code)
    except Exception as e:
        _wxqr.update({'uuid': '', 'image': '', 'code': ''})
        return {'ok': True, 'status': 0, 'loggedIn': False,
                'error': '微信授权码换取登录态失败：%s' % e}
    musickey = str(d.get('musickey') or '')
    musicid = str(d.get('musicid') or '')
    if not musickey:
        _wxqr.update({'uuid': '', 'image': '', 'code': ''})
        return {'ok': True, 'status': 0, 'loggedIn': False,
                'error': '微信登录未返回 musickey（code=%s，%s）'
                         % (rc, d.get('errtip') or '该微信未绑定 QQ 音乐')}
    st = load_state()
    jar = st['cookies']
    jar['qm_keyst'] = musickey
    jar['qqmusic_key'] = musickey
    jar['musickey'] = musickey
    if musicid:
        jar['uin'] = musicid
        st['uin'] = musicid
    st['loginTime'] = int(time.time())
    st['wxKey'] = {'refresh_token': d.get('refresh_token') or '',
                   'refresh_key': d.get('refresh_key') or '',
                   'expired_at': d.get('expired_at') or 0,
                   'keyExpiresIn': d.get('keyExpiresIn') or 0}
    save_state()
    _wxqr.update({'uuid': '', 'image': '', 'code': ''})
    probe = _probe_key()
    prof = qq_profile()
    if not prof.get('nick') and d.get('nick'):
        prof['nick'] = d.get('nick')
        prof['avatar'] = d.get('logo') or prof.get('avatar') or ''
    prof['hasKey'] = True
    _dbg('wx 登录完成 musicid=%s probe=%s' % (musicid, probe))
    if probe.get('checked') and not probe.get('ok'):
        return {'ok': True, 'status': 0, 'loggedIn': True, 'profile': prof, 'hasKey': True,
                'warning': '已登录，但 VIP 播放地址仍换取失败（result=%s），该账号可能不是 VIP'
                           % probe.get('result')}
    return {'ok': True, 'status': 0, 'msg': '登录成功', 'loggedIn': True,
            'profile': prof, 'hasKey': True}


def _follow_chain(url, referer='https://y.qq.com/', max_hops=12):
    for i in range(max_hops):
        try:
            st, hd, body, final = http(url, headers={'Referer': referer}, follow=False)
        except Exception as e:
            _dbg('chain hop%d 失败 %s' % (i, e))
            return
        loc = ''
        try:
            loc = hd.get('Location') or ''
        except Exception:
            pass
        if not loc and st == 200 and body:
            page = _text(body)
            jm = re.search(r'(?:location\.(?:href|replace)\s*=\s*|url\s*=\s*)[\'"]([^\'"]+)[\'"]', page)
            if jm:
                loc = jm.group(1).replace('\\/', '/')
                if loc.startswith('//'):
                    loc = 'https:' + loc
        names = []
        try:
            names = [str(r).split(';')[0].split('=')[0].strip() for r in (hd.get_all('Set-Cookie') or [])]
        except Exception:
            pass
        _dbg('chain hop%d %s loc=%s set=%s' % (i, st, loc[:150], '|'.join(names)))
        if not loc:
            _dbg('chain 结束于 %s' % url[:120])
            return
        if st not in (200, 301, 302, 303, 307, 308):
            _dbg('chain 结束于 %s' % url[:120])
            return
        nxt = urllib.parse.urljoin(url, loc)
        if nxt == url:
            _dbg('chain 自跳转，停止')
            return
        url = nxt
        referer = 'https://y.qq.com/' if 'y.qq.com' in url else 'https://graph.qq.com/'


def _bkn(skey):
    e = 5381
    for ch in str(skey):
        e += (e << 5) + ord(ch)
    return e & 0x7fffffff


def _qq_login_by_code(code):
    if not code:
        return False
    sk = cookies().get('p_skey') or cookies().get('skey') or ''
    payload = {
        'comm': {'g_tk': _bkn(sk), 'platform': 'yqq', 'ct': 24, 'cv': 0},
        'req': {'module': 'QQConnectLogin.LoginServer', 'method': 'QQLogin',
                'param': {'code': code}}
    }
    try:
        st, hd, body, _ = http('https://u.y.qq.com/cgi-bin/musicu.fcg',
                               data=json.dumps(payload).encode('utf-8'), method='POST',
                               headers={'Content-Type': 'application/x-www-form-urlencoded',
                                        'Referer': 'https://y.qq.com/'},
                               use_cookies=False)
    except Exception as e:
        _dbg('QQLogin 请求失败 %s' % e)
        return False
    txt = _text(body)
    _dbg('QQLogin %s %s' % (st, txt[:220].replace('\n', ' ')))
    try:
        d = json.loads(txt or '{}')
    except Exception:
        return False
    data = ((d.get('req') or {}).get('data') or {})
    mk = str(data.get('musickey') or data.get('music_key') or '')
    if not mk:
        return False
    stt = load_state()
    jar = stt['cookies']
    jar['qm_keyst'] = mk
    jar['qqmusic_key'] = mk
    jar['musickey'] = mk
    mid = str(data.get('musicid') or data.get('uin') or '')
    if mid:
        stt['uin'] = mid.lstrip('o')
    save_state()
    _dbg('QQLogin 拿到 musickey')
    return True


def _graph_oauth_finish():
    if _music_key():
        return True
    rd = 'https://y.qq.com/portal/wx_redirect.html'
    u = ('https://graph.qq.com/oauth2.0/authorize?response_type=code&client_id=%s'
         '&redirect_uri=%s&state=%s&scope=get_user_info&display=pc'
         % (PT_3RD_AID, urllib.parse.quote(rd, safe=''), str(int(time.time() * 1000))))
    try:
        st, hd, body, final = http(u, headers={'Referer': 'https://y.qq.com/'}, follow=True)
    except Exception as e:
        _dbg('oauth authorize 页失败 %s' % e)
        return False
    page = _text(body)
    need = re.findall(r'Q\.isNeedLogin\s*=\s*(\w+)', page)
    _dbg('oauth authorize 页 %s final=%s len=%d needLogin=%s'
         % (st, final[:120], len(page), need[-1:] or '未知'))
    fm = re.search(r'<form[^>]+action=[\'"]([^\'"]+)[\'"]', page)
    if not fm:
        _dbg('oauth 未找到授权表单（QQ 互联未认可当前登录态），页面片段=%s'
             % page[:200].replace('\n', ' '))
        return False
    action = fm.group(1)
    if action.startswith('//'):
        action = 'https:' + action
    elif action.startswith('/'):
        action = 'https://graph.qq.com' + action
    fields = {}
    for m1 in re.finditer(r'<input[^>]*>', page):
        tag = m1.group(0)
        nm = re.search(r'name=[\'"]([^\'"]+)[\'"]', tag)
        if not nm:
            continue
        vm = re.search(r'value=[\'"]([^"\']*)[\'"]', tag)
        fields[nm.group(1)] = vm.group(1) if vm else ''
    if not fields.get('redirect_uri'):
        fields['redirect_uri'] = rd
    fields.setdefault('response_type', 'code')
    fields.setdefault('client_id', PT_3RD_AID)
    fields.setdefault('scope', 'get_user_info')
    fields['checkbox'] = '1'
    if not fields.get('g_tk'):
        sk = cookies().get('p_skey') or cookies().get('skey') or ''
        if sk:
            fields['g_tk'] = str(_bkn(sk))
    _dbg('oauth 授权字段=%s' % ','.join(sorted(fields.keys())))
    data = urllib.parse.urlencode(fields).encode('utf-8')
    try:
        st2, hd2, body2, final2 = http(action, data=data, method='POST', follow=False,
                                       headers={'Referer': 'https://graph.qq.com/',
                                                'Content-Type': 'application/x-www-form-urlencoded',
                                                'Origin': 'https://graph.qq.com'})
    except Exception as e:
        _dbg('oauth 授权失败 %s' % e)
        return False
    loc = hd2.get('Location') or ''
    _dbg('oauth authorize %s loc=%s' % (st2, loc[:200]))
    if not loc:
        t2 = _text(body2)
        m2 = re.search(r'(https?://[^\s\'"]+code=[^\s\'"&]+)', t2)
        if m2:
            loc = m2.group(1)
            _dbg('oauth 从页面提取 code 地址')
    if not loc:
        return False
    cm = re.search(r'code=([0-9A-Za-z_\-]+)', loc)
    if cm and _qq_login_by_code(cm.group(1)):
        return True
    _follow_chain(loc, referer='https://graph.qq.com/')
    return _ensure_music_key()


def _finish_login(jump_url, raw_text=''):
    _dbg('=== 扫码成功，开始换取登录态 ===')
    _dbg('jump=%s' % jump_url[:300])
    _dbg('raw=%s' % str(raw_text)[:300].replace('\n', ' '))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(jump_url).query)
    uin = (q.get('uin') or [''])[0]
    ptsigx = (q.get('ptsigx') or [''])[0]
    if not uin:
        m2 = re.search(r'uin=(o?[0-9A-Fa-f]+)', str(raw_text))
        uin = m2.group(1) if m2 else ''
    if not ptsigx:
        m3 = re.search(r'ptsigx=([0-9A-Za-z_\-%]+)', str(raw_text))
        ptsigx = urllib.parse.unquote(m3.group(1)) if m3 else ''
    hops = []
    if uin:
        st = load_state()
        st['uin'] = uin.lstrip('o')
        st['loginTime'] = int(time.time())
        save_state()
        _dbg('记录 uin=%s' % uin)
    if uin and jump_url.startswith('http'):
        try:
            _follow_chain(jump_url)
        except Exception as e:
            _dbg('jump 失败 %s' % e)
        hops.append('跳转地址')
    if not _music_key() and uin and ptsigx:
        cs = ('https://ssl.ptlogin2.graph.qq.com/check_sig?pttype=1&uin=%s&service=ptqrlogin'
              '&nodirect=0&ptsigx=%s&s_url=%s&f_url=&ptlang=2052&ptredirect=100&aid=%s&daid=383'
              '&j_later=0&low_login_hour=0&regmaster=0&pt_3rd_aid=%s&pt_login_sig='
              '&ptqrlogin=1&pt_ptredirect=1'
              % (uin, urllib.parse.quote(ptsigx, safe=''),
                 urllib.parse.quote(U1, safe=''),
                 APPID, PT_3RD_AID))
        try:
            _follow_chain(cs)
        except Exception as e:
            _dbg('check_sig 失败 %s' % e)
        hops.append('check_sig')
    if not _music_key():
        try:
            if _graph_oauth_finish():
                hops.append('QQ互联授权')
        except Exception as e:
            _dbg('oauth 异常 %s' % e)
    _ensure_music_key()
    hops.append('y.qq.com')
    _qr['sig'] = ''
    _qr['image'] = ''
    prof = qq_profile()
    keys = sorted(k for k in cookies().keys() if k)
    _dbg('cookie=%s' % ('、'.join(keys) or '空'))
    _dbg('music_key=%s profile=%s' % (bool(_music_key()), json.dumps(prof, ensure_ascii=False)))
    if not prof.get('loggedIn'):
        return {'ok': True, 'status': 0, 'msg': '登录态未生效', 'loggedIn': False,
                'profile': prof,
                'error': '已扫码，但未取到 QQ 音乐登录 cookie（已尝试：%s；当前 cookie：%s）'
                         % ('、'.join(hops) or '无', '、'.join(keys) or '空'),
                'trace': _trace[-8:]}
    prof['hasKey'] = bool(_music_key())
    return {'ok': True, 'status': 0, 'msg': '登录成功',
            'loggedIn': True, 'profile': prof, 'hasKey': prof['hasKey']}


def _find_vip(obj):
    hit = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower()
                if lk in ('isvip', 'is_vip', 'isgreenvip', 'isbigvip', 'vipflag', 'isyearvip'):
                    if v in (1, '1', True, 'true'):
                        hit.append(True)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return bool(hit)


def _qq_avatar(uin):
    if re.match(r'^\d+$', str(uin or '')):
        return 'https://q1.qlogo.cn/g?b=qq&nk=%s&s=140' % uin
    return ''


def qq_profile():
    uin = _uin()
    has_key = bool(_music_key())
    _dbg('profile uin=%r has_key=%s' % (uin, has_key))
    if not uin:
        return {'loggedIn': False, 'nick': '', 'vip': False, 'uin': '', 'avatar': '',
                'hasKey': has_key}
    url = ('https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_profile_homepage.fcg?format=json'
           '&cid=205360838&ct=20&userid=%s&reqfrom=1&reqtype=0' % uin)
    nick = ''
    vip = False
    avatar = ''
    try:
        raw = http(url, headers={'Referer': 'https://y.qq.com/'})[2]
        _dbg('profile body=%s' % _text(raw)[:180].replace('\n', ' '))
        j = _json_body(raw)
        data = j.get('data') or {}
        creator = data.get('creator') or {}
        nick = creator.get('nick') or creator.get('name') or ''
        vip = _find_vip(data) or _find_vip(j.get('vipInfo') or {})
        avatar = (creator.get('headpic') or creator.get('avatar') or creator.get('headurl') or '')
    except Exception as e:
        _dbg('profile 请求失败 %s' % e)
    if not nick:
        try:
            u2 = ('https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss?hostuin=%s&format=json'
                  '&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json'
                  '&needNewCode=1&sin=0&size=1' % uin)
            j2 = _json_body(http(u2, headers={'Referer': 'https://y.qq.com/'})[2])
            d2 = j2.get('data') or {}
            nick = str(d2.get('hostname') or '').strip()
            _dbg('profile 兜底 hostname=%r' % nick)
        except Exception as e:
            _dbg('profile 兜底失败 %s' % e)
    if not avatar:
        avatar = _qq_avatar(uin)
    logged = bool(nick) or has_key
    return {'loggedIn': logged, 'nick': nick, 'vip': vip, 'uin': uin, 'avatar': avatar,
            'hasKey': has_key}


def qq_playlists():
    uin = _uin()
    _dbg('playlists uin=%r' % uin)
    if not uin:
        return {'ok': False, 'error': '未登录'}
    url = ('https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss?hostuin=%s&format=json'
           '&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json'
           '&needNewCode=1&sin=0&size=200' % uin)
    raw = http(url, headers={'Referer': 'https://y.qq.com/'})[2]
    _dbg('playlists body=%s' % _text(raw)[:180].replace('\n', ' '))
    j = _json_body(raw)
    data = j.get('data') or {}
    out = []
    for it in (data.get('disslist') or []):
        pid = str(it.get('dissid') or it.get('tid') or it.get('diss_id') or '')
        if not pid or pid == '0':
            continue
        out.append({
            'id': pid,
            'title': (it.get('dissname') or it.get('diss_name') or it.get('title') or ''),
            'count': int(it.get('song_cnt') or it.get('songnum') or it.get('song_count') or 0),
            'cover': it.get('logo') or it.get('diss_cover') or it.get('picurl') or ''
        })
    return {'ok': True, 'playlists': out}


def qq_playlist(disstid):
    url = ('https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?type=1&json=1&utf8=1'
           '&onlysong=0&new_format=1&disstid=%s&format=json&inCharset=utf8&outCharset=utf-8'
           '&notice=0&platform=yqq.json&needNewCode=1'
           % urllib.parse.quote(str(disstid)))
    ref = 'https://y.qq.com/n/yqq/playsquare/%s.html' % disstid
    j = _json_body(http(url, headers={'Referer': ref})[2])
    cds = j.get('cdlist') or []
    if not cds:
        return {'ok': False, 'error': '歌单读取失败'}
    cd = cds[0]
    songs = []
    for s in (cd.get('songlist') or []):
        mid = s.get('songmid') or s.get('mid') or ''
        if not mid:
            continue
        singers = ' / '.join([x.get('name', '') for x in (s.get('singer') or []) if x.get('name')])
        album = s.get('albumname') or (s.get('album') or {}).get('name') or ''
        albmid = s.get('albummid') or (s.get('album') or {}).get('mid') or ''
        pay = s.get('pay') or {}
        songs.append({
            'songmid': mid,
            'mediaMid': s.get('media_mid') or s.get('strMediaMid') or mid,
            'title': s.get('songname') or s.get('name') or s.get('title') or '',
            'artist': singers or '未知歌手',
            'album': album,
            'duration': int(s.get('interval') or 0),
            'cover': ('https://y.qq.com/music/photo_new/T002R300x300M000%s.jpg' % albmid) if albmid else '',
            'vip': bool(pay.get('payplay'))
        })
    return {'ok': True, 'title': cd.get('dissname') or '', 'songs': songs}


def _song_item(s):
    mid = s.get('songmid') or s.get('mid') or ''
    if not mid:
        return None
    singers = ' / '.join([x.get('name', '') for x in (s.get('singer') or [])
                          if isinstance(x, dict) and x.get('name')])
    if not singers:
        singers = s.get('singer_name') or s.get('singername') or ''
    album = s.get('albumname') or (s.get('album') or {}).get('name') or ''
    albmid = s.get('albummid') or (s.get('album') or {}).get('mid') or ''
    pay = s.get('pay') or {}
    return {
        'songmid': mid,
        'mediaMid': s.get('media_mid') or s.get('strMediaMid') or mid,
        'title': s.get('songname') or s.get('name') or s.get('title') or '',
        'artist': singers or '未知歌手',
        'album': album,
        'duration': int(s.get('interval') or 0),
        'cover': ('https://y.qq.com/music/photo_new/T002R300x300M000%s.jpg' % albmid) if albmid else '',
        'vip': bool(pay.get('payplay')) or bool(s.get('pay_play'))
    }


def qq_search(kw, page=1, size=20):
    kw = str(kw or '').strip()
    if not kw:
        return {'ok': False, 'error': '请输入搜索关键词'}
    url = ('https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp?format=json&inCharset=utf8'
           '&outCharset=utf-8&notice=0&platform=h5&needNewCode=1&w=%s&zhidaqu=1&catZhida=1'
           '&t=0&flag=1&ie=utf-8&sem=1&aggr=0&perpage=%d&n=%d&p=%d&remoteplace=txt.mqq.all&uin=0'
           % (urllib.parse.quote(kw), int(size), int(size), int(page)))
    j = _json_body(http(url, headers={'Referer': 'https://y.qq.com/'})[2])
    data = j.get('data') or {}
    songs = []
    for it in ((data.get('song') or {}).get('list') or []):
        s = _song_item(it)
        if s:
            songs.append(s)
    return {'ok': True, 'keyword': kw, 'songs': songs}


def _vkey_once(songmid, mm, uin, platform, guid):
    names = ['M500%s.mp3' % mm, 'C400%s.m4a' % mm, 'F000%s.flac' % mm, 'M800%s.mp3' % mm]
    comm = {'uin': uin, 'format': 'json', 'ct': 24, 'cv': 0}
    key = _music_key()
    if key and uin != '0':
        comm['authst'] = key
    payload = {
        'req': {
            'module': 'CDN.SrfCdnDispatchServer',
            'method': 'GetCdnDispatch',
            'param': {'guid': guid, 'calltype': 0, 'userip': ''}
        },
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'guid': guid,
                'songmid': [songmid] * len(names),
                'songtype': [0] * len(names),
                'filename': names,
                'uin': uin,
                'loginflag': 1,
                'platform': platform
            }
        },
        'comm': comm
    }
    data = json.dumps(payload).encode('utf-8')
    st, hd, body, _ = http('https://u.y.qq.com/cgi-bin/musicu.fcg', data=data, method='POST',
                           use_cookies=(uin != '0'),
                           headers={'Referer': 'https://y.qq.com/',
                                    'Content-Type': 'application/json'})
    j = _json_body(body)
    r = (j.get('req_0') or {}).get('data') or {}
    purl = ''
    result = 0
    for info in (r.get('midurlinfo') or []):
        if info.get('purl'):
            purl = info['purl']
            result = int(info.get('result') or 0)
            break
        if not result:
            result = int(info.get('result') or 0)
    return purl, result, (r.get('sip') or [])


PROBE_SONG = '002w57E00BGzXn'


def _probe_key():
    if not _music_key():
        return {'checked': False, 'ok': False, 'reason': 'no_key'}
    try:
        guid = load_state().get('guid') or str(random.randint(1000000, 9999999999))
        purl, result, _sip = _vkey_once(PROBE_SONG, PROBE_SONG, _uin() or '0', '20', guid)
    except Exception as e:
        return {'checked': False, 'ok': False, 'reason': 'probe_error: %s' % e}
    return {'checked': True, 'ok': bool(purl), 'result': result}


MEDIA_MID_CACHE = {}
STREAM_CACHE = {}
STREAM_CACHE_TTL = 60


def qq_media_mid(songmid):
    songmid = str(songmid or '').strip()
    if not songmid:
        return ''
    if songmid in MEDIA_MID_CACHE:
        return MEDIA_MID_CACHE[songmid]
    mm = ''
    try:
        payload = {'comm': {'ct': 24, 'cv': 0, 'uin': _uin() or '0'},
                   'req': {'module': 'music.pf_song_detail_svr',
                           'method': 'get_song_detail',
                           'param': {'song_mid': songmid}}}
        st, hd, body, _ = http('https://u.y.qq.com/cgi-bin/musicu.fcg',
                               data=json.dumps(payload).encode('utf-8'),
                               method='POST',
                               headers={'Referer': 'https://y.qq.com/',
                                        'Content-Type': 'application/json'})
        j = _json_body(body)
        tr = (((j.get('req') or {}).get('data') or {}).get('track_info') or {})
        mm = str((tr.get('file') or {}).get('media_mid') or '').strip()
    except Exception as e:
        _dbg('media_mid 解析失败 %s %s' % (songmid, e))
    if mm:
        MEDIA_MID_CACHE[songmid] = mm
    return mm


def _cdn_alive(url):
    if not url:
        return False
    try:
        st, hd, body, _ = http(url, headers={'Range': 'bytes=0-1'},
                               use_cookies=False, timeout=12)
    except Exception:
        return False
    try:
        return 200 <= int(st or 0) < 300
    except Exception:
        return False


def _vkey_lookup(songmid, mm, uin, guid):
    attempts = [(uin, '20'), ('0', '20'), ('0', '11')]
    purl = ''
    result = 0
    sip = []
    used = uin
    for auin, plat in attempts:
        try:
            purl, result, s = _vkey_once(songmid, mm, auin, plat, guid)
        except Exception as e:
            _dbg('vkey 尝试失败 uin=%s plat=%s %s' % (auin, plat, e))
            continue
        if s:
            sip = s
        if purl:
            used = auin
            break
    return purl, result, sip, used


def _cdn_url(purl, sip):
    if not purl:
        return ''
    base = sip[0] if sip else 'https://dl.stream.qqmusic.qq.com/'
    return base.rstrip('/') + '/' + purl.lstrip('/')


def qq_stream_detail(songmid, media_mid):
    ck = (str(songmid or ''), str(media_mid or ''))
    hit = STREAM_CACHE.get(ck)
    if hit and time.time() - hit[0] < STREAM_CACHE_TTL:
        return dict(hit[1])
    info = _qq_stream_detail(songmid, media_mid)
    STREAM_CACHE[ck] = (time.time(), dict(info))
    return info


def _qq_stream_detail(songmid, media_mid):
    st = load_state()
    guid = st.get('guid')
    if not guid:
        guid = str(random.randint(1000000, 9999999999))
        st['guid'] = guid
        save_state()
    uin = _uin() or '0'
    mm = media_mid or songmid
    if mm == songmid:
        mm = qq_media_mid(songmid) or mm
    purl, result, sip, used = _vkey_lookup(songmid, mm, uin, guid)
    url = _cdn_url(purl, sip)
    file_dead = False
    if url and not _cdn_alive(url):
        real = qq_media_mid(songmid)
        if real and real != mm:
            _dbg('media_mid 校正 %s -> %s' % (mm, real))
            mm = real
            purl, result, sip2, used = _vkey_lookup(songmid, mm, uin, guid)
            if sip2:
                sip = sip2
            url = _cdn_url(purl, sip)
        if url and not _cdn_alive(url):
            url = ''
            file_dead = True
    has_key = bool(_music_key())
    vip_codes = (104003, 104004, 104006, 104007, 104008)
    info = {'songmid': songmid, 'mediaMid': mm, 'uin': used,
            'hasKey': has_key, 'purl': purl, 'result': result,
            'needVip': (not purl) and (result in vip_codes or (result == 0 and not has_key)),
            'sip': sip[:2], 'url': url, 'fileDead': file_dead}
    _dbg('stream %s uin=%s key=%s result=%s dead=%s url=%s'
         % (songmid, used, bool(_music_key()), result, file_dead,
            (info['url'] or '-')[:120]))
    return info


def qq_stream_url(songmid, media_mid):
    return qq_stream_detail(songmid, media_mid).get('url') or ''


def qq_find_playable(kw, exclude=''):
    kw = str(kw or '').strip()
    if not kw:
        return {'ok': False, 'error': '缺少关键词'}
    r = qq_search(kw, 1, 12)
    songs = r.get('songs') or []
    tried = []
    for s in songs:
        mid = s.get('songmid') or ''
        if not mid or mid == exclude:
            continue
        tried.append(s.get('title') or mid)
        try:
            info = qq_stream_detail(mid, s.get('mediaMid') or mid)
        except Exception:
            continue
        if info.get('url'):
            return {'ok': True, 'song': s, 'url': info['url'], 'tried': tried}
        if len(tried) >= 6:
            break
    return {'ok': False, 'error': '没有找到可播放的版本', 'tried': tried}


# ---------------- 歌词（QQ 音乐 / 网易云，带内存缓存） ----------------
_LYRIC_CACHE = {}
_LYRIC_TTL = 6 * 3600


def _lyric_cache_get(key):
    it = _LYRIC_CACHE.get(key)
    if not it:
        return None
    if time.time() - it[0] > _LYRIC_TTL:
        _LYRIC_CACHE.pop(key, None)
        return None
    return it[1]


def _lyric_cache_put(key, val):
    if len(_LYRIC_CACHE) > 400:
        _LYRIC_CACHE.clear()
    _LYRIC_CACHE[key] = (time.time(), val)


def _maybe_base64(s):
    s = (s or '').strip()
    if not s or '[' in s:
        return s
    try:
        raw = base64.b64decode(s)
        t = raw.decode('utf-8', 'ignore')
    except Exception:
        return s
    return t if '[' in t else s


def qq_lyric(songmid):
    songmid = str(songmid or '').strip()
    if not songmid:
        return {'ok': False, 'error': '缺少歌曲 mid'}
    hit = _lyric_cache_get('qq:' + songmid)
    if hit:
        return hit
    url = ('https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=%s'
           '&g_tk=5381&loginUin=0&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8'
           '&notice=0&platform=yqq.json&needNewCode=0&nobase64=1'
           % urllib.parse.quote(songmid))
    st, hd, body, _ = http(url, headers={'Referer': 'https://y.qq.com/'})
    txt = _text(body).strip()
    m = re.match(r'[A-Za-z_$][\w$.]*\(([\s\S]*)\)\s*;?$', txt)
    if m:
        txt = m.group(1)
    try:
        j = json.loads(txt) if txt else {}
    except Exception:
        _dbg('QQ歌词解析失败 %s %s' % (songmid, txt[:120]))
        return {'ok': False, 'error': '歌词解析失败', 'id': songmid}
    lyric = _maybe_base64(j.get('lyric') or '')
    trans = _maybe_base64(j.get('trans') or '')
    out = {'ok': bool(lyric), 'id': songmid, 'lyric': lyric, 'trans': trans,
           'retcode': j.get('retcode')}
    if lyric:
        _lyric_cache_put('qq:' + songmid, out)
    _dbg('QQ歌词 %s 行数=%d retcode=%s' % (songmid, lyric.count('\n'), j.get('retcode')))
    return out


def ne_lyric(sid):
    sid = str(sid or '').strip()
    if not sid:
        return {'ok': False, 'error': '缺少歌曲 id'}
    hit = _lyric_cache_get('ne:' + sid)
    if hit:
        return hit
    j = _json_body(ne_http('https://music.163.com/api/song/lyric?id=%s&lv=1&kv=1&tv=-1' % sid))
    lyric = (j.get('lrc') or {}).get('lyric') or ''
    trans = (j.get('tlyric') or {}).get('lyric') or ''
    out = {'ok': bool(lyric), 'id': sid, 'lyric': lyric, 'trans': trans}
    if lyric:
        _lyric_cache_put('ne:' + sid, out)
    _dbg('网易云歌词 %s 行数=%d' % (sid, lyric.count('\n')))
    return out


# ---------------- 二维码生成（纯标准库，网易云扫码用） ----------------
_QR_EXP = [0] * 512
_QR_LOG = [0] * 256


def _qr_gf_init():
    x = 1
    for i in range(255):
        _QR_EXP[i] = x
        _QR_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _QR_EXP[i] = _QR_EXP[i - 255]


_qr_gf_init()

# 版本 -> (总码字, 数据码字, 每块纠错码字, 块数)，纠错等级 L
_QR_L = {
    1: (26, 19, 7, 1), 2: (44, 34, 10, 1), 3: (70, 55, 15, 1), 4: (100, 80, 20, 1),
    5: (134, 108, 26, 1), 6: (172, 136, 18, 2), 7: (196, 156, 20, 2), 8: (242, 194, 24, 2),
}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
             7: [6, 22, 38], 8: [6, 24, 42]}
_QR_VER_INFO = {7: 0x07C94, 8: 0x085BC}


def _qr_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _QR_EXP[_QR_LOG[a] + _QR_LOG[b]]


def _qr_poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a == 0:
            continue
        for j, b in enumerate(q):
            if b:
                r[i + j] ^= _qr_mul(a, b)
    return r


def _qr_ecc(block, n):
    gen = [1]
    for i in range(n):
        gen = _qr_poly_mul(gen, [1, _QR_EXP[i]])
    buf = list(block) + [0] * n
    for i in range(len(block)):
        coef = buf[i]
        if coef:
            for j, g in enumerate(gen):
                if g:
                    buf[i + j] ^= _qr_mul(g, coef)
    return buf[len(block):]


def _qr_format(ecc_bits, mask):
    data = (ecc_bits << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _qr_matrix(text):
    data = text.encode('utf-8')
    ver = 0
    for v in sorted(_QR_L):
        nd = _QR_L[v][1]
        if len(data) <= (nd * 8 - 12) // 8:
            ver = v
            break
    if not ver:
        raise RuntimeError('二维码内容过长')
    total, ndata, ecpb, blocks = _QR_L[ver]

    bits = [0, 1, 0, 0]
    for i in range(7, -1, -1):
        bits.append((len(data) >> i) & 1)
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    need = ndata * 8
    bits.extend([0] * min(4, max(0, need - len(bits))))
    while len(bits) % 8:
        bits.append(0)
    cws = []
    for i in range(0, len(bits), 8):
        val = 0
        for b in bits[i:i + 8]:
            val = (val << 1) | b
        cws.append(val)
    pad = [0xEC, 0x11]
    k = 0
    while len(cws) < ndata:
        cws.append(pad[k % 2])
        k += 1

    per = ndata // blocks
    dblocks = [cws[i * per:(i + 1) * per] for i in range(blocks)]
    eblocks = [_qr_ecc(b, ecpb) for b in dblocks]
    out = []
    for i in range(per):
        for b in dblocks:
            out.append(b[i])
    for i in range(ecpb):
        for b in eblocks:
            out.append(b[i])

    size = 17 + 4 * ver
    mod = [[0] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def put(r, c, v):
        mod[r][c] = 1 if v else 0
        fn[r][c] = True

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < size and 0 <= cc < size:
                    dark = (0 <= r <= 6 and 0 <= c <= 6 and
                            (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)))
                    put(rr, cc, 1 if dark else 0)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(size):
        if not fn[6][i]:
            put(6, i, 0 if i % 2 else 1)
        if not fn[i][6]:
            put(i, 6, 0 if i % 2 else 1)

    cen = _QR_ALIGN[ver]
    ln = len(cen)
    for i in range(ln):
        for j in range(ln):
            if (i == 0 and j == 0) or (i == 0 and j == ln - 1) or (i == ln - 1 and j == 0):
                continue
            ar, ac = cen[i], cen[j]
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(ar + dr, ac + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)

    if ver >= 7:
        vb = _QR_VER_INFO[ver]
        for i in range(18):
            bit = (vb >> i) & 1
            a, b = size - 11 + i % 3, i // 3
            put(b, a, bit)
            put(a, b, bit)

    fmt_order = []
    for i in range(6):
        fmt_order.append((i, 8, i))
    fmt_order.append((7, 8, 6))
    fmt_order.append((8, 8, 7))
    fmt_order.append((8, 7, 8))
    for i in range(9, 15):
        fmt_order.append((8, 14 - i, i))
    for i in range(8):
        fmt_order.append((8, size - 1 - i, i))
    for i in range(8, 15):
        fmt_order.append((size - 15 + i, 8, i))
    for r, c, _i in fmt_order:
        fn[r][c] = True
    fn[size - 8][8] = True

    bitstream = []
    for v in out:
        for i in range(7, -1, -1):
            bitstream.append((v >> i) & 1)

    idx = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = (size - 1 - vert) if upward else vert
                if not fn[row][col] and idx < len(bitstream):
                    mod[row][col] = bitstream[idx]
                    idx += 1
        right -= 2

    def draw_format(m, mask):
        fb = _qr_format(1, mask)
        for r, c, i in fmt_order:
            m[r][c] = (fb >> i) & 1
        m[size - 8][8] = 1

    def apply_mask(mask):
        m = [row[:] for row in mod]
        for r in range(size):
            for c in range(size):
                if fn[r][c]:
                    continue
                if mask == 0:
                    inv = (r + c) % 2 == 0
                elif mask == 1:
                    inv = r % 2 == 0
                elif mask == 2:
                    inv = c % 3 == 0
                elif mask == 3:
                    inv = (r + c) % 3 == 0
                elif mask == 4:
                    inv = (r // 2 + c // 3) % 2 == 0
                elif mask == 5:
                    inv = (r * c) % 2 + (r * c) % 3 == 0
                elif mask == 6:
                    inv = ((r * c) % 2 + (r * c) % 3) % 2 == 0
                else:
                    inv = ((r + c) % 2 + (r * c) % 3) % 2 == 0
                if inv:
                    m[r][c] ^= 1
        draw_format(m, mask)
        return m

    def penalty(m):
        score = 0
        for r in range(size):
            run = 1
            for c in range(1, size):
                if m[r][c] == m[r][c - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
        for c in range(size):
            run = 1
            for r in range(1, size):
                if m[r][c] == m[r - 1][c]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
        for r in range(size - 1):
            for c in range(size - 1):
                s = m[r][c] + m[r][c + 1] + m[r + 1][c] + m[r + 1][c + 1]
                if s == 0 or s == 4:
                    score += 3
        pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        pat_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
        for r in range(size):
            for c in range(size - 10):
                seg = m[r][c:c + 11]
                if seg == pat_a or seg == pat_b:
                    score += 40
        for c in range(size):
            for r in range(size - 10):
                seg = [m[r + i][c] for i in range(11)]
                if seg == pat_a or seg == pat_b:
                    score += 40
        dark = sum(sum(row) for row in m)
        pct = dark * 100 // (size * size)
        score += 10 * (abs(pct - 50) // 5)
        return score

    best = None
    for mask in range(8):
        m = apply_mask(mask)
        sc = penalty(m)
        if best is None or sc < best[0]:
            best = (sc, m)
    return best[1]


def _qr_png(text, scale=0, border=4):
    m = _qr_matrix(text)
    n = len(m)
    scale = scale or max(3, 240 // (n + border * 2))
    total = (n + border * 2) * scale
    raw = bytearray()
    for y in range(total):
        my = y // scale - border
        raw.append(0)
        for x in range(total):
            mx = x // scale - border
            raw.append(0 if (0 <= my < n and 0 <= mx < n and m[my][mx]) else 255)
    z = zlib.compress(bytes(raw), 9)

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload +
                struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', total, total, 8, 0, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
            chunk(b'IDAT', z) + chunk(b'IEND', b''))


# ---------------- 网易云音乐 ----------------
NE_FILE = os.path.join(ROOT, 'netease_session.json')
NE_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
NE_QR_TTL = 100
NE_QR_GRACE = 360
_ne = {'key': '', 'time': 0, 'image': '', 'grace': []}
_ne_state = {}


def ne_load():
    global _ne_state
    if _ne_state.get('_loaded'):
        return _ne_state
    data = {}
    try:
        with open(NE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get('cookies'), dict):
        data['cookies'] = {}
    data['_loaded'] = True
    _ne_state = data
    return _ne_state


def ne_save():
    st = ne_load()
    out = {k: v for k, v in st.items() if not k.startswith('_')}
    try:
        with open(NE_FILE, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def ne_cookies():
    return ne_load().get('cookies') or {}


def ne_header():
    jar = dict(ne_cookies())
    jar.setdefault('os', 'pc')
    jar.setdefault('appver', '8.9.70')
    return '; '.join('%s=%s' % (k, v) for k, v in jar.items())


def ne_merge(raw_list):
    st = ne_load()
    jar = st['cookies']
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
        ne_save()
    return changed


def ne_http(url, data=None, method='GET', headers=None, timeout=20, send_cookies=True):
    hdrs = {'User-Agent': NE_UA, 'Referer': 'https://music.163.com/', 'Accept': '*/*'}
    if send_cookies:
        hdrs['Cookie'] = ne_header()
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
        ne_merge(sets)
        status = getattr(resp, 'status', None) or getattr(resp, 'code', 0)
        loc = resp.headers.get('Location') if resp.headers else None
        if status in (301, 302, 303, 307, 308) and loc and hops < 4:
            hops += 1
            url = urllib.parse.urljoin(url, loc)
            try:
                resp.close()
            except Exception:
                pass
            data = None
            method = 'GET'
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
        return body


def ne_profile():
    j = {}
    try:
        j = _json_body(ne_http('https://music.163.com/api/nuser/account/get'))
    except Exception as e:
        _dbg('网易云账号读取失败 %s' % e)
    prof = j.get('profile') or {}
    uid = str(prof.get('userId') or '')
    if not uid:
        return {'loggedIn': False, 'nick': '', 'vip': False, 'uin': '', 'avatar': '',
                'hasKey': False, 'provider': 'ne'}
    return {'loggedIn': True, 'nick': prof.get('nickname') or '', 'vip': int(prof.get('vipType') or 0) > 0,
            'uin': uid, 'avatar': prof.get('avatarUrl') or '',
            'hasKey': True, 'provider': 'ne'}


def ne_warmup_visitor():
    st = ne_load()
    st['cookies'].pop('NMTID', None)
    st['cookies'].pop('__csrf', None)
    st['cookies'].pop('JSESSIONID-WYYY', None)
    ne_save()
    try:
        ne_http('https://music.163.com/', method='GET', send_cookies=True, timeout=15)
    except Exception as e:
        _dbg('网易云访客预热失败 %s' % e)
    try:
        ne_http('https://music.163.com/api/song/enhance/player/url?ids=%5B1%5D&br=999000&os=pc&appver=8.9.70',
                method='GET', send_cookies=True, timeout=15)
    except Exception:
        pass


def ne_qr_create():
    now = time.time()
    if _ne.get('key') and _ne.get('image') and now - (_ne.get('time') or 0) < NE_QR_TTL:
        return {'ok': True, 'image': _ne['image'], 'reused': True}
    ne_warmup_visitor()
    body = ne_http('https://music.163.com/api/login/qrcode/unikey?os=pc&appver=8.9.70',
                   data=b'type=1', method='POST',
                   headers={'Content-Type': 'application/x-www-form-urlencoded'},
                   send_cookies=True)
    j = _json_body(body)
    key = str(j.get('unikey') or '')
    if not key:
        raise RuntimeError('网易云二维码获取失败：%s' % _text(body)[:120])
    url = 'https://music.163.com/login?codekey=' + key
    png = _qr_png(url)
    image = 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
    old = _ne.get('key')
    if old and old != key:
        gr = [g for g in (_ne.get('grace') or []) if now - g['time'] < NE_QR_GRACE]
        gr.append({'key': old, 'time': _ne.get('time') or now})
        _ne['grace'] = gr[-3:]
    _ne.update({'key': key, 'time': now, 'image': image})
    _dbg('网易云二维码已生成 key=%s' % key)
    return {'ok': True, 'image': image}


def ne_login_by_key(key, body, j):
    _ne.update({'key': '', 'image': '', 'grace': []})
    _dbg('网易云扫码 803 key=%s body=%s' % (key, _text(body)[:400]))
    ck = str(j.get('cookie') or '')
    if ck:
        ne_merge(re.split(r'[;,]', ck))
    if not ne_cookies().get('MUSIC_U'):
        return {'ok': False, 'status': 65, 'loggedIn': False,
                'error': '扫码已确认，但网易云没有返回登录凭证（MUSIC_U），请重新扫码'}
    st = ne_load()
    st['loginTime'] = int(time.time())
    ne_save()
    prof = {}
    for i in range(3):
        prof = ne_profile()
        if prof.get('loggedIn'):
            break
        time.sleep(0.4)
    if prof.get('loggedIn'):
        st['uin'] = prof.get('uin') or ''
        ne_save()
    _dbg('网易云登录完成 %s' % json.dumps(prof, ensure_ascii=False))
    return {'ok': True, 'status': 0, 'msg': '登录成功', 'loggedIn': bool(prof.get('loggedIn')),
            'profile': prof, 'hasKey': bool(prof.get('loggedIn'))}


NE_QR_HOSTS = ('https://music.163.com', 'https://interface.music.163.com')
NE_QR_CODES = (800, 801, 802, 803, 8821)


def _ne_qr_poll_once(host, key, method):
    qs = '?os=pc&appver=8.9.70'
    if method == 'POST':
        body = ne_http(host + '/api/login/qrcode/client/login' + qs,
                       data=('key=%s&type=1' % key).encode('utf-8'),
                       method='POST', send_cookies=True,
                       headers={'Content-Type': 'application/x-www-form-urlencoded'})
    else:
        body = ne_http(host + '/api/login/qrcode/client/login' + qs + '&type=1&key=' + key,
                       send_cookies=True)
    j = _json_body(body)
    return int(j.get('code') or 0), body, j


def ne_qr_poll_key(key):
    code, body, j = 0, b'', {}
    for host in NE_QR_HOSTS:
        for method in ('POST', 'GET'):
            try:
                c, b, jj = _ne_qr_poll_once(host, key, method)
            except Exception as e:
                _dbg('网易云扫码轮询失败 host=%s %s err=%s' % (host, method, e))
                continue
            code, body, j = c, b, jj
            if c in NE_QR_CODES:
                break
        if code in NE_QR_CODES and code != 8821:
            break
    if code not in (801, 802):
        _dbg('网易云扫码轮询 key=%s code=%s body=%s' % (key, code, _text(body)[:200]))
    return code, body, j


def ne_risk(j):
    return {'ok': False, 'status': 69, 'loggedIn': False, 'risk': True,
            'captchaId': str(j.get('captchaId') or ''),
            'redirect': str(j.get('redirectUrl') or ''),
            'error': '网易云风控拦截（8821）：%s' % (j.get('message') or '请切换其他登录方式或升级新版本再试')}


def ne_qr_check():
    now = time.time()
    key = _ne.get('key')
    code, body, j = 0, b'', {}
    if key:
        code, body, j = ne_qr_poll_key(key)
    if code == 803:
        return ne_login_by_key(key, body, j)
    if code == 8821:
        _ne.update({'key': '', 'image': ''})
        return ne_risk(j)
    if code == 800:
        _ne.update({'key': '', 'image': ''})

    seen = 802 if code == 802 else 0
    gr = [g for g in (_ne.get('grace') or []) if now - g['time'] < NE_QR_GRACE]
    if code in (0, 800, 801):
        for g in reversed(gr):
            try:
                gc, gb, gj = ne_qr_poll_key(g['key'])
            except Exception:
                continue
            if gc == 803:
                _dbg('网易云旧二维码补登成功 key=%s' % g['key'])
                return ne_login_by_key(g['key'], gb, gj)
            if gc == 8821:
                return ne_risk(gj)
            if gc == 802:
                seen = 802
    _ne['grace'] = [g for g in (_ne.get('grace') or []) if now - g['time'] < NE_QR_GRACE]

    if seen == 802:
        return {'ok': True, 'status': 67, 'msg': '已扫码，请在手机上确认', 'loggedIn': False}
    if not key:
        return {'ok': True, 'status': 65, 'msg': '二维码已过期，请重新获取', 'loggedIn': False}
    if code == 800:
        return {'ok': True, 'status': 65, 'msg': '二维码已过期，请重新获取', 'loggedIn': False}
    return {'ok': True, 'status': 66, 'msg': j.get('message') or '等待扫码', 'loggedIn': False}


def ne_logout():
    st = ne_load()
    st['cookies'] = {}
    st.pop('uin', None)
    ne_save()
    _ne.update({'key': '', 'image': ''})
    return {'ok': True}


def _ne_song(t):
    if not isinstance(t, dict):
        return None
    sid = str(t.get('id') or '')
    if not sid:
        return None
    artists = ' / '.join([a.get('name', '') for a in (t.get('ar') or t.get('artists') or [])
                          if isinstance(a, dict) and a.get('name')])
    al = t.get('al') or t.get('album') or {}
    dt = t.get('dt') or t.get('duration') or 0
    fee = int(t.get('fee') or 0)
    return {'songmid': sid, 'mediaMid': sid,
            'title': t.get('name') or '',
            'artist': artists or '未知歌手',
            'album': al.get('name') or '',
            'duration': int(dt / 1000) if dt else 0,
            'cover': al.get('picUrl') or '',
            'vip': fee in (1, 4)}


def ne_fill_covers(songs, limit=24):
    ids = [s['songmid'] for s in songs[:limit] if s.get('songmid') and not s.get('cover')]
    if not ids:
        return songs
    try:
        j = _json_body(ne_http('https://music.163.com/api/song/detail?ids=[%s]' % ','.join(ids)))
    except Exception as e:
        _dbg('网易云封面补全失败 %s' % e)
        return songs
    m = {}
    for t in (j.get('songs') or []):
        al = t.get('album') or {}
        if al.get('picUrl'):
            m[str(t.get('id'))] = al['picUrl']
    for s in songs:
        if not s.get('cover') and s.get('songmid') in m:
            s['cover'] = m[s['songmid']]
    return songs


def ne_playlists():
    prof = ne_profile()
    uid = prof.get('uin') or ''
    if not uid:
        return {'ok': False, 'error': '未登录网易云音乐'}
    j = _json_body(ne_http('https://music.163.com/api/user/playlist?uid=%s&limit=1000&offset=0' % uid))
    out = []
    for it in (j.get('playlist') or []):
        out.append({'id': str(it.get('id') or ''),
                    'title': it.get('name') or '',
                    'count': int(it.get('trackCount') or 0),
                    'cover': it.get('coverImgUrl') or ''})
    return {'ok': True, 'playlists': out}


def ne_playlist(pid):
    pid = str(pid or '').strip()
    if not pid:
        return {'ok': False, 'error': '缺少歌单 id'}
    j = _json_body(ne_http('https://music.163.com/api/v6/playlist/detail?id=%s&n=1000&s=0' % pid))
    pl = j.get('playlist') or {}
    if not pl:
        return {'ok': False, 'error': '歌单读取失败'}
    songs = [s for s in (_ne_song(t) for t in (pl.get('tracks') or [])) if s]
    return {'ok': True, 'title': pl.get('name') or '', 'songs': ne_fill_covers(songs)}


def ne_sub_albums():
    prof = ne_profile()
    if not prof.get('loggedIn'):
        return {'ok': False, 'error': '未登录网易云音乐'}
    out = []
    try:
        j = _json_body(ne_http('https://music.163.com/api/album/sublist?limit=1000&offset=0'))
        for it in (j.get('data') or []):
            ar = it.get('artist') or {}
            out.append({'id': str(it.get('id') or ''),
                        'title': it.get('name') or '',
                        'artist': ar.get('name') or '',
                        'cover': it.get('picUrl') or '',
                        'count': int(it.get('size') or 0)})
    except Exception as e:
        _dbg('网易云收藏专辑读取失败 %s' % e)
    return {'ok': True, 'albums': out}


def ne_purchased_albums():
    prof = ne_profile()
    if not prof.get('loggedIn'):
        return {'ok': False, 'error': '未登录网易云音乐'}
    out = []
    try:
        j = _json_body(ne_http('https://music.163.com/api/digitalAlbum/purchased?limit=1000&offset=0'))
        data = j.get('data') or {}
        for it in (data.get('list') or data.get('albums') or []):
            ar = it.get('artist') or {}
            if isinstance(ar, list):
                ar = ar[0] if ar else {}
            out.append({'id': str(it.get('id') or ''),
                        'title': it.get('name') or '',
                        'artist': ar.get('name') or '',
                        'cover': it.get('picUrl') or '',
                        'count': int(it.get('size') or 0)})
    except Exception as e:
        _dbg('网易云已购专辑读取失败 %s' % e)
    return {'ok': True, 'albums': out}


def ne_collection():
    sub = ne_sub_albums()
    pur = ne_purchased_albums()
    return {'ok': True, 'subAlbums': sub.get('albums') or [], 'purchasedAlbums': pur.get('albums') or []}


def ne_album_tracks(aid):
    aid = str(aid or '').strip()
    if not aid:
        return {'ok': False, 'error': '缺少专辑 id'}
    j = _json_body(ne_http('https://music.163.com/api/album/%s' % aid))
    al = j.get('album') or {}
    if not al:
        return {'ok': False, 'error': '专辑读取失败'}
    songs = [s for s in (_ne_song(t) for t in (al.get('songs') or [])) if s]
    ar = al.get('artist') or {}
    if isinstance(ar, list):
        ar = ar[0] if ar else {}
    return {'ok': True,
            'title': al.get('name') or '',
            'artist': ar.get('name') or '',
            'cover': al.get('picUrl') or '',
            'songs': ne_fill_covers(songs)}


def ne_search(kw, limit=20):
    kw = str(kw or '').strip()
    if not kw:
        return {'ok': False, 'error': '请输入搜索关键词'}
    url = ('https://music.163.com/api/search/get/web?s=%s&type=1&limit=%d&offset=0'
           % (urllib.parse.quote(kw), int(limit)))
    j = _json_body(ne_http(url))
    songs = [s for s in (_ne_song(t) for t in ((j.get('result') or {}).get('songs') or [])) if s]
    return {'ok': True, 'keyword': kw, 'songs': ne_fill_covers(songs)}


def ne_stream_detail(sid):
    sid = str(sid or '').strip()
    if not sid:
        return {'ok': False, 'error': '缺少歌曲 id'}
    j = _json_body(ne_http('https://music.163.com/api/song/enhance/player/url?ids=[%s]&br=320000' % sid))
    data = j.get('data') or []
    item = data[0] if data else {}
    purl = item.get('url') or ''
    _dbg('网易云 stream %s code=%s url=%s' % (sid, item.get('code'), (purl or '-')[:100]))
    return {'ok': bool(purl), 'id': sid, 'url': purl, 'code': item.get('code'),
            'hasKey': bool(ne_cookies().get('MUSIC_U')), 'needVip': not purl}


def ne_find_playable(kw, exclude=''):
    kw = str(kw or '').strip()
    if not kw:
        return {'ok': False, 'error': '缺少关键词'}
    r = ne_search(kw, 12)
    tried = []
    for s in r.get('songs') or []:
        sid = s.get('songmid') or ''
        if not sid or sid == exclude:
            continue
        tried.append(s.get('title') or sid)
        try:
            info = ne_stream_detail(sid)
        except Exception:
            continue
        if info.get('url'):
            return {'ok': True, 'song': s, 'url': info['url'], 'tried': tried}
        if len(tried) >= 6:
            break
    return {'ok': False, 'error': '没有找到可播放的版本', 'tried': tried}


class Handler(SimpleHTTPRequestHandler):
    server_version = 'WayVMusicBridge/1.0'
    protocol_version = 'HTTP/1.1'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        print('  %s  %s' % (self.log_date_time_string(), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _guard(self, fn):
        try:
            return self._json(fn())
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)
        path = p.path
        if path == '/api/qq/status':
            return self._guard(lambda: {'ok': True, 'profile': qq_profile(),
                                        'loginTime': load_state().get('loginTime', 0)})
        if path == '/api/qq/diag':
            return self._guard(self._diag)
        if path == '/api/qq/qr':
            return self._guard(qq_qr_create)
        if path == '/api/qq/qr/check':
            return self._guard(qq_qr_check)
        if path == '/api/qq/wxqr':
            return self._guard(wx_qr_create)
        if path == '/api/qq/wxqr/check':
            return self._guard(wx_qr_check)
        if path == '/api/qq/playlists':
            return self._guard(qq_playlists)
        if path == '/api/qq/playlist':
            did = (q.get('id') or [''])[0]
            return self._guard(lambda: qq_playlist(did))
        if path == '/api/qq/search':
            kw = (q.get('kw') or [''])[0]
            return self._guard(lambda: qq_search(kw))
        if path == '/api/qq/find-playable':
            kw = (q.get('kw') or [''])[0]
            ex = (q.get('exclude') or [''])[0]
            return self._guard(lambda: qq_find_playable(kw, ex))
        if path == '/api/qq/stream':
            mid = (q.get('songmid') or [''])[0]
            mmid = (q.get('mediaMid') or [mid])[0]
            dbg = (q.get('debug') or [''])[0] in ('1', 'true', 'yes')
            return self._stream(mid, mmid, dbg)
        if path == '/api/qq/cover':
            return self._cover((q.get('url') or [''])[0])
        if path == '/api/qq/lyric':
            return self._guard(lambda: qq_lyric((q.get('songmid') or q.get('id') or [''])[0]))
        if path == '/api/ne/status':
            return self._guard(lambda: {'ok': True, 'profile': ne_profile(),
                                        'loginTime': ne_load().get('loginTime', 0)})
        if path == '/api/ne/diag':
            return self._guard(lambda: {'ok': True, 'key': _ne.get('key') or '',
                                        'keyAge': int(time.time() - (_ne.get('time') or 0)),
                                        'cookies': sorted(ne_cookies().keys()),
                                        'hasMusicU': bool(ne_cookies().get('MUSIC_U')),
                                        'uin': ne_load().get('uin') or '',
                                        'loginTime': ne_load().get('loginTime', 0)})
        if path == '/api/ne/qr':
            return self._guard(ne_qr_create)
        if path == '/api/ne/qr/check':
            return self._guard(ne_qr_check)
        if path == '/api/ne/playlists':
            return self._guard(ne_playlists)
        if path == '/api/ne/playlist':
            did = (q.get('id') or [''])[0]
            return self._guard(lambda: ne_playlist(did))
        if path == '/api/ne/collection':
            return self._guard(ne_collection)
        if path == '/api/ne/album':
            aid = (q.get('id') or [''])[0]
            return self._guard(lambda: ne_album_tracks(aid))
        if path == '/api/ne/search':
            kw = (q.get('kw') or [''])[0]
            return self._guard(lambda: ne_search(kw))
        if path == '/api/ne/find-playable':
            kw = (q.get('kw') or [''])[0]
            ex = (q.get('exclude') or [''])[0]
            return self._guard(lambda: ne_find_playable(kw, ex))
        if path == '/api/ne/stream':
            return self._ne_stream((q.get('id') or [''])[0],
                                   (q.get('debug') or [''])[0] in ('1', 'true', 'yes'))
        if path == '/api/ne/cover':
            return self._cover((q.get('url') or [''])[0])
        if path == '/api/ne/lyric':
            return self._guard(lambda: ne_lyric((q.get('id') or q.get('songmid') or [''])[0]))
        if path.startswith('/api/'):
            return self._json({'ok': False, 'error': '未知接口'}, 404)
        return super().do_GET()

    def _cover(self, url):
        if not url.startswith('http'):
            return self._json({'ok': False, 'error': '缺少封面地址'}, 400)
        try:
            st, hd, body, _ = http(url, headers={'Referer': 'https://y.qq.com/'}, use_cookies=False)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 502)
        ctype = 'image/jpeg'
        if body[:4] == b'\x89PNG':
            ctype = 'image/png'
        elif body[:4] == b'RIFF':
            ctype = 'image/webp'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, songmid, media_mid, debug=False):
        if not songmid:
            return self._json({'ok': False, 'error': '缺少 songmid'}, 400)
        try:
            info = qq_stream_detail(songmid, media_mid)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)
        if debug:
            info['ok'] = bool(info.get('url'))
            return self._json(info)
        if not info.get('url'):
            need_vip = bool(info.get('needVip'))
            if info.get('fileDead'):
                error = ('该曲目的音频文件不存在或已下架（media_mid=%s），'
                         '换一首同名版本试试' % info.get('mediaMid'))
            elif need_vip:
                error = '该曲目需要 VIP 或数字专辑，当前账号无法播放（result=%s）' % info.get('result')
            else:
                error = ('该曲目暂时没有可用播放地址（result=%s，可能已下架或版权受限）'
                         % info.get('result'))
            return self._json({'ok': False,
                               'error': error,
                               'needVip': need_vip,
                               'result': info.get('result')})
        self.send_response(302)
        self.send_header('Location', info['url'])
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self._cors()
        self.end_headers()

    def _ne_stream(self, sid, debug=False):
        if not sid:
            return self._json({'ok': False, 'error': '缺少歌曲 id'}, 400)
        try:
            info = ne_stream_detail(sid)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)
        if debug:
            info['ok'] = bool(info.get('url'))
            return self._json(info)
        if not info.get('url'):
            return self._json({'ok': False, 'needVip': True,
                               'error': '该曲目需要 VIP 或版权受限，当前账号无法播放'})
        self.send_response(302)
        self.send_header('Location', info['url'])
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self._cors()
        self.end_headers()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == '/api/qq/cookie':
            return self._guard(self._import_cookie)
        if p.path == '/api/qq/logout':
            return self._guard(self._logout)
        if p.path == '/api/ne/logout':
            return self._guard(ne_logout)
        return self._json({'ok': False, 'error': '未知接口'}, 404)

    def _read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n else b'{}'
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return {}

    def _import_cookie(self):
        data = self._read_json()
        parts = []
        raw = str(data.get('cookie') or '')
        if raw:
            parts.append(raw)
        uin = str(data.get('uin') or '').strip()
        keyst = str(data.get('keyst') or '').strip()
        if uin:
            parts.append('uin=' + uin)
        if keyst:
            parts.append('qm_keyst=' + keyst)
        text = '; '.join(parts)
        text = text.replace('\r', '').replace('\n', ';').strip()
        text = re.sub(r'(?i)^cookie\s*:\s*', '', text)
        st = load_state()
        jar = st['cookies']
        for part in text.split(';'):
            if '=' not in part:
                continue
            k, v = part.split('=', 1)
            k = urllib.parse.unquote(k.strip())
            v = v.strip().strip('"')
            if '%' in v:
                try:
                    dec = urllib.parse.unquote(v)
                    if dec.isprintable():
                        v = dec
                except Exception:
                    pass
            if k and v:
                jar[k] = v
        if jar.get('uin'):
            st['uin'] = _clean_uin(jar['uin'])
        st['loginTime'] = int(time.time())
        save_state()
        _ensure_music_key()
        probe = _probe_key()
        if probe.get('checked') and not probe.get('ok'):
            for k in KEY_KEYS:
                jar.pop(k, None)
            save_state()
            return {'ok': False,
                    'error': '凭证无效或该账号没有 VIP 权限（result=%s），请确认复制的是已登录 VIP 账号的 qm_keyst'
                             % probe.get('result'),
                    'hasKey': False,
                    'profile': qq_profile(), 'trace': _trace[-6:]}
        prof = qq_profile()
        if not prof.get('loggedIn'):
            keys = sorted(k for k in jar.keys() if k)
            return {'ok': False, 'error': 'Cookie 无效或已过期（当前 cookie：%s）'
                    % ('、'.join(keys) or '空'),
                    'profile': prof, 'trace': _trace[-8:]}
        return {'ok': True, 'profile': prof, 'hasKey': bool(_music_key())}

    def _diag(self):
        ck = cookies()
        keys = sorted(k for k in ck.keys() if k)
        return {'ok': True,
                'uin': _uin(),
                'musicKey': bool(_music_key()),
                'musicKeyName': next((k for k in KEY_KEYS if ck.get(k)), ''),
                'cookieNames': keys,
                'profile': qq_profile(),
                'qrActive': bool(_qr.get('sig')),
                'qrAge': int(time.time() - (_qr.get('time') or 0)) if _qr.get('time') else -1,
                'loginTime': load_state().get('loginTime', 0),
                'trace': _trace[-20:]}

    def _logout(self):
        global _state
        try:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        except Exception:
            pass
        _state = {'_loaded': True, 'cookies': {}}
        _qr['sig'] = ''
        return {'ok': True}


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except Exception:
            pass
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print('QQ 音乐桥接服务已启动：http://localhost:%d/index.html' % port)
    print('登录态文件：%s' % SESSION_FILE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        srv.server_close()


if __name__ == '__main__':
    main()