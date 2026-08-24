from remote_collect import scrape_cn_homepage
r = scrape_cn_homepage(5)
print(f"Got {len(r)} articles")
for a in r:
    t = a["title"][:40]
    has_img = bool(a.get("image"))
    desc = a.get("summary", "")[:40]
    print(f"  {t}  img={has_img}  desc={desc}")
