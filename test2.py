import json
import os
import aiohttp
import asyncio

# 默认 SUID
DEFAULT_SUID = "0"

# hash.json 路径
HASH_FILE = "hash.json"

# 读取 hash.json
if not os.path.exists(HASH_FILE):
    print(f"⚠️ {HASH_FILE} 不存在")
    hash_map = {}
else:
    with open(HASH_FILE, "r", encoding="utf-8") as f:
        hash_map = json.load(f)

# 测试用 device_id 列表
device_ids = ["609529", "609528", "276577", "999999"]  # 可自行修改

API_URL = "https://api.powerliber.com/client/1/device/detail"
TOKEN = "token"

async def fetch_ports(device_ids):
    ports_data = {}
    async with aiohttp.ClientSession() as session:
        for device_id in device_ids:
            suid = hash_map.get(device_id, DEFAULT_SUID)
            print(suid)
            if suid == DEFAULT_SUID:
                print(f"device_id={device_id} 未找到 SUID，返回空列表")
                ports_data[device_id] = []
                continue

            payload = {
                "token": TOKEN,
                "client_id": 1,
                "app_id": "dd",
                "suid": suid
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0"
            }

            try:
                async with session.post(API_URL, data=payload, headers=headers) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        print(f"device_id={device_id}, suid={suid}, 接口返回错误: {data}")
                        ports_data[device_id] = []
                        continue

                    device = data.get("data", {}).get("device")
                    if not device:
                        ports_data[device_id] = []
                        continue

                    port_list = json.loads(device.get("port_list", "[]"))
                    ports_data[device_id] = [
                        {
                            "port_index": p.get("port_index"),
                            "charge_status": p.get("charge_status", 0),
                            "energy": p.get("energy_consumed", 0),
                            "power": p.get("power", 0)
                        } for p in port_list
                    ]

            except Exception as e:
                print(f"device_id={device_id}, 请求失败: {e}")
                ports_data[device_id] = []

    print("\n最终 ports_data:")
    print(json.dumps(ports_data, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(fetch_ports(device_ids))
