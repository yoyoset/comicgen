import re
import urllib.request

for idx in ("cu124", "cu121"):
    url = f"https://abetlen.github.io/llama-cpp-python/whl/{idx}/llama-cpp-python/"
    try:
        h = urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "ignore")
    except Exception as e:
        print(idx, "err", e)
        continue
    whls = re.findall(r'href="([^"]+\.whl)"', h)
    print(idx, "总:", len(whls))
    print("  cp313:", [w for w in whls if "cp313" in w][:3])
    print("  win_ANY:", [w for w in whls if "win_amd64" in w][:3])