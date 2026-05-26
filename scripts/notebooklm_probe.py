import subprocess, json, urllib.request

report = {}

# CDP version
try:
    r = urllib.request.urlopen("http://localhost:9250/json/version", timeout=5)
    report['cdp_version'] = json.loads(r.read().decode())
except Exception as e:
    report['cdp_version_error'] = str(e)

# Tab list
try:
    r = urllib.request.urlopen("http://localhost:9250/json/list", timeout=5)
    tabs = json.loads(r.read().decode())
    report['tab_count'] = len(tabs)
    # Filter to relevant
    short = []
    for t in tabs:
        short.append({'url': t.get('url',''), 'title': t.get('title',''), 'type': t.get('type','')})
    report['tabs'] = [t for t in short if t['type'] == 'page'][:30]
    nlm = [t for t in tabs if 'notebooklm' in t.get('url','').lower()]
    report['notebooklm_tabs'] = [{'url': t['url'], 'title': t['title']} for t in nlm]
except Exception as e:
    report['tab_list_error'] = str(e)

try:
    import playwright
    report['playwright_version'] = playwright.__version__
except Exception as e:
    report['playwright_error'] = str(e)

print(json.dumps(report, indent=2))
