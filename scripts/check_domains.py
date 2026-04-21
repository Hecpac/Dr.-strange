import urllib.request

domains = [
    "pachanodesign.com",
    "premiumhome.design",
    "www.premiumhome.design",
    "sinpetca.com",
    "www.sinpetca.com",
    "tcinsurancetx.com",
    "www.tcinsurancetx.com",
]

for d in domains:
    try:
        req = urllib.request.Request(f"https://{d}", method="HEAD")
        req.add_header("User-Agent", "ClawCheck/1.0")
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"{d}: {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"{d}: {e.code}")
    except Exception as e:
        print(f"{d}: DOWN ({e})")
