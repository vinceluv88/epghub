from jinja2 import Environment, FileSystemLoader
from epg import utils
from epg.generator import xmltv
from epg.generator import diyp
from epg.scraper import __xmltv
from lxml import etree
from datetime import datetime, timezone
from croniter import croniter
import os
import shutil

CF_PAGES = os.getenv("CF_PAGES")
CF_PAGES_URL = os.getenv("CF_PAGES_URL")
DEPLOY_HOOK = os.getenv("DEPLOY_HOOK")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
XMLTV_URL = os.getenv("XMLTV_URL", "")
TZ = os.getenv("TZ")
if TZ is None:
    print(
        "!!!Please set TZ environment variables to define timezone or it will use system timezone by default!!!"
    )
CRON_TRIGGER = os.getenv("CRON_TRIGGER", "0 0 * * *")
next_cron_time = (
    croniter(CRON_TRIGGER, datetime.now(timezone.utc))
    .get_next(datetime)
    .replace(tzinfo=timezone.utc)
    .astimezone()
)

dtd = etree.DTD(open("xmltv.dtd", "r"))

now = datetime.now()
current_timezone = now.astimezone().tzinfo
timezone_name = current_timezone.tzname(now) if current_timezone else "UTC"
timezone_offset = now.astimezone().strftime("%z")
print("use timezone:", timezone_name, f"UTC{timezone_offset}", flush=True)

config_path = os.path.join(os.getcwd(), "config", "channels.yaml")
epg_path = os.path.join(os.getcwd(), "web", "epg.xml")
if not os.path.exists(os.path.join(os.getcwd(), "web")):
    os.mkdir(os.path.join(os.getcwd(), "web"))

channels = utils.load_config(config_path)

if XMLTV_URL == "":
    xml_channels = []
    print("!!!Please set XMLTV_URL environment variables to reuse XML!!!")
else:
    print("reuse XML:", XMLTV_URL, flush=True)
    xml_channels = __xmltv.get_channels(XMLTV_URL, dtd)
    # Reuse channels
    if xml_channels != []:
        xml_result = utils.copy_channels(channels, xml_channels)
        num_reuse_channels = xml_result[0]
        xml_dates = xml_result[1]
        if xml_dates:
            min_xml_date = min(xml_dates)
            max_xml_date = max(xml_dates)
        else:
            print("xml_dates is empty")
            min_xml_date = None
            max_xml_date = None
        print(
            f"number of reused channels: {num_reuse_channels}/{len(channels)} from {min_xml_date} to {max_xml_date}",
            flush=True,
        )

print("refreshing...")

num_refresh_channels = 0
for channel in channels:
    if utils.update_channel_full(channel, num_refresh_channels):
        num_refresh_channels += 1

print(
    f"number of refreshed channels: {num_refresh_channels}/{len(channels)}", flush=True
)

print("deploying...", flush=True)
print("file path:", epg_path, flush=True)
xmltv.write(epg_path, channels, "epghub")

xml = open(epg_path, "rb")
root = etree.XML(xml.read())
valid = dtd.validate(root)
if not valid:
    print(dtd.error_log.filter_from_errors()[0])

diyp.write(os.path.join(os.getcwd(), "web", "diyp_files"), channels)

# Load the template
templateLoader = FileSystemLoader(searchpath=os.path.join(os.getcwd(), "templates"))
env = Environment(loader=templateLoader)
template = env.get_template("index.html.jinja2")

title = "myTV SUPER 節目表"
channel_list = [channel.metadata["name"][0] for channel in channels]
first_channel = channel_list[0]
channel_list = channel_list[1:]
# Convert CRON_TRIGGER next cron time to datetime type
next_update_time = next_cron_time

# Render the template with the list
rendered_html = template.render(
    title=title,
    channel_list=channel_list,
    first_channel=first_channel,
    num_refresh_channels=num_refresh_channels,
    num_channels=len(channels),
    last_update_time=datetime.now().astimezone().isoformat(timespec="seconds"),
    next_update_time=next_update_time,
    update_trigger=CRON_TRIGGER,
    timezone_offset=timezone_offset,
)

open(os.path.join(os.getcwd(), "web", "index.html"), "w").write(rendered_html)
shutil.copyfile(
    os.path.join(os.getcwd(), "templates", "404.html"),
    os.path.join(os.getcwd(), "web", "404.html"),
)
shutil.copyfile(
    os.path.join(os.getcwd(), "templates", "404.json"),
    os.path.join(os.getcwd(), "web", "404.json"),
)
shutil.copyfile(
    os.path.join(os.getcwd(), "templates", "robots.txt"),
    os.path.join(os.getcwd(), "web", "robots.txt"),
)

# ==================== 🛠️ 以下為修正後的 Cloudflare Worker 自動部署定時器邏輯 ====================
if CF_PAGES is not None:
    if CLOUDFLARE_API_TOKEN is None or DEPLOY_HOOK is None:
        print("!!!請檢查：DEPLOY_HOOK 或 CLOUDFLARE_API_TOKEN 環境變數缺失，無法配置自動更新!!!")
    else:
        print("正在透過 Python API 自動配置 Cloudflare Worker 定時鬧鐘...", flush=True)
        import requests
        import json
        
        headers = {
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json"
        }
        
        try:
            # 1. 獲取您的 Cloudflare 帳戶 ID (Account ID)
            acc_res = requests.get("https://cloudflare.com", headers=headers).json()
            if acc_res.get("success") and acc_res.get("result"):
                # 取得列表中的第一個帳戶 ID
                account_id = acc_res["result"][0]["id"]
                worker_name = "epghub-scheduler"
                
                # 2. 編寫 Worker 鬧鐘腳本（時間到時自動發送 POST 請求給 Pages Deploy Hook）
                worker_script = f"""
                export default {{
                  async scheduled(event, env, ctx) {{
                    await fetch("{DEPLOY_HOOK}", {{ method: "POST" }});
                  }}
                }};
                """
                
                # 3. 上傳並部署 Worker 腳本
                upload_url = f"https://cloudflare.com/{account_id}/workers/scripts/{worker_name}"
                metadata = {"main_module": "index.js"}
                files = {
                    "metadata": (None, json.dumps(metadata), "application/json"),
                    "index.js": (None, worker_script, "application/javascript")
                }
                
                # 獨立建立不含 JSON Content-Type 的 Header，讓 requests 自動帶入 multipart boundary
                upload_headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
                res = requests.put(upload_url, headers=upload_headers, files=files).json()
                
                if res.get("success"):
                    print(f"成功部署 Worker 腳本: {worker_name}", flush=True)
                    
                    # 4. 為這個 Worker 綁定 Cron 定時器
                    trigger_url = f"https://cloudflare.com/{account_id}/workers/scripts/{worker_name}/triggers"
                    trigger_data = [{"cron": CRON_TRIGGER}]
                    cron_res = requests.put(trigger_url, headers=headers, json=trigger_data).json()
                    
                    if cron_res.get("success"):
                        print(f"🎉 成功同步自動更新定時器 [{CRON_TRIGGER}] 到 Cloudflare！", flush=True)
                    else:
                        print(f"❌ 定時器綁定失敗，原因: {cron_res.get('errors')}")
                else:
                    print(f"❌ Worker 腳本上傳失敗，原因: {res.get('errors')}")
            else:
                print(f"❌ 無法獲取 Cloudflare 帳戶 ID，請確認 Token 權限是否包含 [帳戶-Cloudflare Pages-編輯]、[帳戶-Worker指令碼-編輯]、[使用者-成員資格-讀取]。")
        except Exception as e:
            print(f"❌ 自動配置 Worker 發生異常: {str(e)}")
# ==================== 🛠️ 修改結束 ====================
