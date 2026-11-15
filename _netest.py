import json
import urllib.request
import urllib.parse

B = 'http://127.0.0.1:8000'


def g(p, t=25):
    r = urllib.request.urlopen(B + p, timeout=t)
    return json.loads(r.read())


print('1) 取二维码')
j = g('/api/wyymusic/qr')
print('   ok=%s reused=%s img_len=%s' % (j.get('ok'), j.get('reused'), len(j.get('image') or '')))

print('2) 直接调网易云接口，看原始响应')
import importlib.util
spec = importlib.util.spec_from_file_location('m', r'd:\WayV Vision\qqmusic_server.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

key = m._ne.get('key')
print('   key=%r' % key)
raw = m.ne_http('https://music.163.com/api/login/qrcode/client/login?type=1&key=' + key, send_cookies=False)
print('   raw=', raw[:300])

print('3) 走桥接轮询接口')
for i in range(3):
    print('   ', g('/api/wyymusic/qr/check'))

print('4) 当前会话文件内容')
print('   ', json.dumps({k: (list(v.keys()) if isinstance(v, dict) else v) for k, v in m.ne_load().items()}, ensure_ascii=False))