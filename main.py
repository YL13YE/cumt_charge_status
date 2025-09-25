import json
import os
import aiohttp
import time
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent


@register("astrbot_plugin_charge_status", "YL1EYE", "查询cumt充电桩端口状态", "1.0.3")
class ChargeStationPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = os.path.dirname(__file__)
        self.device_map_path = os.path.join(self.data_dir, "device_map.json")
        self.device_map = self._load_device_map()
        self.hash_path = os.path.join(self.data_dir, "hash.json")
        self.hash_map = self._load_hash_map()
        self.DEFAULT_SUID = "0"
        # 全局缓存 (所有用户共用)
        self.cache = {}

    def _load_device_map(self):
        """从 device_map.json 读取设备映射表"""
        if not os.path.exists(self.device_map_path):
            logger.warning(f"[ChargeStationPlugin] device_map.json 不存在：{self.device_map_path}")
            return {}
        try:
            with open(self.device_map_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 读取 device_map.json 失败: {e}")
            return {}

    def _load_hash_map(self):
        """从 device_map.json 读取设备映射表"""
        if not os.path.exists(self.hash_path):
            logger.warning(f"[ChargeStationPlugin] hash.json 不存在：{self.hash_path}")
            return {}
        try:
            with open(self.hash_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 读取 hash.json 失败: {e}")
            return {}

    def _get_campus_areas(self, campus=None):
        """获取校区下的所有区域列表"""
        areas = []
        target_map = self.device_map

        if campus:
            if campus not in self.device_map:
                return []
            target_map = {campus: self.device_map.get(campus, {})}

        for region, types in target_map.items():
            areas.extend(types.keys())

        return list(set(areas))  # 去重

    def _format_device_map(self, ports_data=None, campus=None, area=None):
        """格式化输出设备映射表，显示端口状态，处理未找到 SUID 的情况"""
        lines = []
        target_map = self.device_map

        if campus:
            target_map = {campus: self.device_map.get(campus, {})}

        for region, types in target_map.items():
            lines.append(f"区域：{region}")
            for type_, devices in types.items():
                if area and type_ != area:
                    continue
                lines.append(f"  类型：{type_}")
                max_len = max((len(name) for name in devices.values()), default=0)
                for device_id, device_name in devices.items():
                    ports_info = ""
                    if ports_data:
                        dev_ports = ports_data.get(str(device_id), [])
                        # 未找到 SUID 或无端口数据
                        if dev_ports == [] and self.hash_map.get(device_id, self.DEFAULT_SUID) == self.DEFAULT_SUID:
                            ports_info = " | 无可用端口 (未找到 SUID)"
                        elif dev_ports:
                            port_statuses = []
                            for port in dev_ports:
                                power = port["power"]
                                energy = port["energy"]
                                if port["charge_status"] == 1:
                                    power = max(100, power)
                                    times = round(energy/power, 1)
                                    status = f"约{times}h"
                                else:
                                    times = 0
                                    status = "空闲"
                                port_statuses.append(f"{port['port_index']}:{status}")
                            ports_info = " | " + ", ".join(port_statuses)
                        else:
                            ports_info = " | 无可用端口"

                    name_padded = device_name.ljust(max_len)
                    lines.append(f"    {name_padded} ({device_id}){ports_info}")

        return "\n".join(lines)

    async def _fetch_ports_data(self, device_ids):
        """根据 device_id 列表获取端口数据，返回适配 /charge 格式"""
        API_URL = "https://api.powerliber.com/client/1/device/detail"
        TOKEN = "token"

        ports_data = {}  # {device_id: [port_info_dict,...]}

        async with aiohttp.ClientSession() as session:
            for device_id in device_ids:
                suid = self.hash_map.get(device_id, self.DEFAULT_SUID)
                if suid == self.DEFAULT_SUID:
                    ports_data[str(device_id)] = []
                    continue

                payload = {
                    "token": TOKEN,
                    "client_id": 1,
                    "app_id": "dd",
                    "suid": suid
                }
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0",
                }

                try:
                    async with session.post(API_URL, data=payload, headers=headers) as resp:
                        data = await resp.json()
                        if data.get("code") != 100000:
                            ports_data[str(device_id)] = []
                            continue

                        device = data.get("data", {}).get("device")
                        if not device:
                            ports_data[str(device_id)] = []
                            continue

                        port_list = json.loads(device.get("port_list", "[]"))
                        ports_data[str(device_id)] = [
                            {
                                "port_index": p.get("port_index"),
                                "charge_status": p.get("charge_status", 0),
                                "energy": p.get("energy_consumed", 0),
                                "power": p.get("power", 0)
                            } for p in port_list
                        ]

                except Exception as e:
                    logger.error(f"[ChargeStationPlugin] 请求 SUID {suid} 接口失败: {e}")
                    ports_data[str(device_id)] = []
        logger.info(ports_data)
        return {"code": 100000, "data": ports_data}
        """请求接口获取端口数据 停用
        url = f"https://lwstools.xyz/api/charge_station/ports?device_ids={','.join(device_ids)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 请求接口失败: {e}")
            return None
        """

    @filter.command("charge")
    async def query_charge(self, event: AstrMessageEvent):
        """指令：/电桩 [校区] [区域]"""
        text = event.get_message_str().strip()
        parts = text.split()
        campus = parts[1] if len(parts) > 1 else None
        area = parts[2] if len(parts) > 2 else None

        # 缓存 key
        cache_key = (campus or "all", area or "all")
        now = time.time()
        cache_entry = self.cache.get(cache_key)

        if cache_entry and now - cache_entry["time"] < 60:
            yield event.plain_result(f"(缓存数据，{int(now - cache_entry['time'])}秒前更新)\n{cache_entry['reply']}")
            return

        # 获取需要查询的 device_ids
        device_ids = []
        target_map = self.device_map
        if campus:
            target_map = {campus: self.device_map.get(campus, {})}
        for types in target_map.values():
            for devices in types.values():
                device_ids.extend(devices.keys())

        if not device_ids:
            yield event.plain_result("未找到设备，请检查校区或区域名称")
            return

        data = await self._fetch_ports_data(device_ids)
        if not data:
            yield event.plain_result("获取充电桩信息失败")
            return
        if data.get("code") != 100000:
            yield event.plain_result("接口返回错误")
            return

        ports_data = data.get("data", {})
        reply = self._format_device_map(ports_data=ports_data, campus=campus, area=area)

        # 更新缓存
        self.cache[cache_key] = {"time": now, "ports_data": ports_data, "reply": reply}

        yield event.plain_result(reply)

    @filter.command("charge_refresh")
    async def refresh_cache(self, event: AstrMessageEvent):
        """强制刷新缓存，获取最新信息"""
        self.cache.clear()
        yield event.plain_result("✅ 缓存已清空，下次查询将强制获取最新数据")

    @filter.command("charge_list")
    async def list_areas(self, event: AstrMessageEvent):
        """列出校区或指定校区的所有区域"""
        text = event.get_message_str().strip()
        parts = text.split()

        if len(parts) < 2:
            campuses = list(self.device_map.keys())
            if not campuses:
                yield event.plain_result("⚠️ 未配置任何校区")
                return

            reply = "🏫 可用校区列表：\n" + "\n".join(f"  - {campus}" for campus in campuses)

            yield event.plain_result(reply)
            return

        campus = parts[1]
        areas = self._get_campus_areas(campus)

        if not areas:
            yield event.plain_result(f"⚠️ 校区「{campus}」不存在或未配置区域")
            return

        max_len = max(len(a) for a in areas)
        area_stats = []
        for area_name in areas:
            device_count = len(self.device_map.get(campus, {}).get(area_name, {}))
            area_stats.append(f"  {area_name.ljust(max_len)} | {device_count:>2} 个设备")

        reply = f"📍 校区「{campus}」的区域列表：\n" + "\n".join(area_stats)
        yield event.plain_result(reply)

    @filter.command("charge_help")
    async def charge_help(self, event: AstrMessageEvent):
        """显示电桩指令帮助信息"""
        help_msg = (
            "充电桩查询指令使用说明：\n"
            "/charge                  显示所有校区所有端口\n"
            "/charge <校区>            显示指定校区所有端口\n"
            "/charge <校区> <区域>      显示指定校区指定区域端口\n"
            "/charge_list      显示指定校区区域列表\n"
            "/charge_refresh          强制清空缓存，下次查询获取最新数据\n"
            "/charge_help             显示帮助信息\n"
        )
        yield event.plain_result(help_msg)

    async def initialize(self):
        logger.info("[ChargeStationPlugin] 插件已初始化")

    async def terminate(self):
        logger.info("[ChargeStationPlugin] 插件已卸载")

