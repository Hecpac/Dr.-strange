import json, urllib.request

try:
    r = urllib.request.urlopen("http://localhost:9250/json/list", timeout=5)
    tabs = json.loads(r.read().decode())
    relevant = []
    for t in tabs:
        u = t.get('url','').lower()
        if any(k in u for k in ['chatgpt.com', 'chat.openai', 'gemini.google', 'sora.com']):
            relevant.append({'url': t.get('url',''), 'title': t.get('title','')})
    print(json.dumps({'matches': relevant, 'total_tabs': len(tabs)}, indent=2))
except Exception as e:
    print(json.dumps({'err': str(e)}))
