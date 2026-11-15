import json, urllib.request

LOG = r'd:\WayV Vision\_probe_out.txt'
lines = []


def get(url, timeout=60):
    return json.loads(urllib.request.urlopen(url, timeout=timeout
                                             ).read().decode('utf-8', 'ignore'))


r = get('http://127.0.0.1:8000/api/qqmusic/search?kw=%E5%91%A8%E6%9D%B0%E5%80%AB')
songs = (r.get('songs') or [])[:10]
lines.append('搜索命中 %d 首，逐首探测：' % len(songs))
ok = 0
for s in songs:
    mid = s.get('songmid')
    mm = s.get('mediaMid') or mid
    try:
        j = get('http://127.0.0.1:8000/api/qqmusic/stream?debug=1&songmid=%s&mediaMid=%s'
                % (mid, mm))
        good = bool(j.get('url'))
        ok += 1 if good else 0
        lines.append('%-5s vip=%-5s result=%-6s dead=%-5s %s'
                     % ('OK' if good else 'FAIL', s.get('vip'), j.get('result'),
                        j.get('fileDead'), (s.get('title') or '')[:22]))
    except Exception as e:
        lines.append('ERR   %s %s' % ((s.get('title') or '')[:22], e))

lines.append('可播放 %d/%d' % (ok, len(songs)))
open(LOG, 'w', encoding='utf-8').write('\n'.join(lines))