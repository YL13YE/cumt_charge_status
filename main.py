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
        """格式化输出设备映射表，端口数量高亮对齐"""
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
                    free_ports_count = 0
                    if ports_data:
                        dev_ports = ports_data.get(str(device_id), [])
                        free_ports_count = len(dev_ports)
                        ports_str = ", ".join(str(p) for p in dev_ports) if dev_ports else "无可用端口"
                        ports_info = f" | 空闲端口({free_ports_count}): {ports_str}"
                    name_padded = device_name.ljust(max_len)
                    lines.append(f"    {name_padded} ({device_id}){ports_info}")
        return "\n".join(lines)

    @filter.command("helloworld")  # from astrbot.api.event.filter import command
    async def helloworld(self, event: AstrMessageEvent):
        '''这是 hello world 指令'''
        user_name = event.get_sender_name()
        message_str = event.message_str  # 获取消息的纯文本内容
        yield event.plain_result(f"Hello, {user_name}!")

    @filter.command("charge")
    async def query_charge(self, event: AstrMessageEvent):
        """指令：/电桩 [校区] [区域]"""
        text = event.get_message_str().strip()
        parts = text.split()
        campus = parts[1] if len(parts) > 1 else None
        area = parts[2] if len(parts) > 2 else None

        # 缓存 key
        cache_key = (campus, area)
        now = time.time()
        cache_entry = self.cache.get(cache_key)

        if cache_entry and now - cache_entry["time"] < 60:
            event.plain_result(f"(缓存数据，{int(now - cache_entry['time'])}秒前更新)\n{cache_entry['reply']}")
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
            event.plain_result("未找到设备，请检查校区或区域名称")
            return

        data = await self._fetch_ports_data(device_ids)
        if not data:
            event.plain_result("获取充电桩信息失败")
            return
        if data.get("code") != 100000:
            event.plain_result("接口返回错误")
            return

        ports_data = data.get("data", {})
        reply = self._format_device_map(ports_data=ports_data, campus=campus, area=area)

        # 更新缓存
        self.cache[cache_key] = {"time": now, "ports_data": ports_data, "reply": reply}

        event.plain_result(reply)

    @filter.command("charge_refresh")
    async def refresh_cache(self, event: AstrMessageEvent):
        """强制刷新缓存，获取最新信息"""
        self.cache.clear()
        event.plain_result("✅ 缓存已清空，下次查询将强制获取最新数据")

    @filter.command("charge_list")
    async def list_areas(self, event: AstrMessageEvent):
        """列出校区或指定校区的所有区域"""
        text = event.get_message_str().strip()
        parts = text.split()

        if len(parts) < 2:
            campuses = list(self.device_map.keys())
            if not campuses:
                event.plain_result("⚠️ 未配置任何校区")
                return

            reply = "🏫 可用校区列表：\n" + "\n".join(f"  - {campus}" for campus in campuses)

            event.plain_result(reply)
            return

        campus = parts[1]
        areas = self._get_campus_areas(campus)

        if not areas:
            event.plain_result(f"⚠️ 校区「{campus}」不存在或未配置区域")
            return

        max_len = max(len(a) for a in areas)
        area_stats = []
        for area_name in areas:
            device_count = len(self.device_map.get(campus, {}).get(area_name, {}))
            area_stats.append(f"  {area_name.ljust(max_len)} | {device_count:>2} 个设备")

        reply = f"📍 校区「{campus}」的区域列表：\n" + "\n".join(area_stats)
        event.plain_result(reply)

    @filter.command("charge_help")
    async def charge_help(self, event: AstrMessageEvent):
        """显示电桩指令帮助信息"""
        help_msg = (
            "充电桩查询指令使用说明：\n"
            "/电桩                  显示所有校区所有端口\n"
            "/电桩 <校区>            显示指定校区所有端口\n"
            "/电桩 <校区> <区域>      显示指定校区指定区域端口\n"
            "/charge_list      显示指定校区区域列表\n"
            "/charge_refresh          强制清空缓存，下次查询获取最新数据\n"
            "/charge_help             显示帮助信息\n"
        )
        event.plain_result(help_msg)

    async def initialize(self):
        logger.info("[ChargeStationPlugin] 插件已初始化")

    async def terminate(self):
        logger.info("[ChargeStationPlugin] 插件已卸载")

    async def _fetch_ports_data(self, device_ids):
        """请求接口获取端口数据"""
        url = f"https://lwstools.xyz/api/charge_station/ports?device_ids={','.join(device_ids)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 请求接口失败: {e}")
            return None
