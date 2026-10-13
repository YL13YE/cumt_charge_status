import json
import os
import time
import html as _html
import unicodedata
from datetime import datetime

import aiohttp
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.message.components import Location, Plain, Json, Image
from astrbot.core.message.message_event_result import MessageChain
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

try:
    from astrbot.api import AstrBotConfig
except Exception:
    AstrBotConfig = None

# AstrBot v3 / v4 的 HTML 渲染入口路径不同，逐个尝试，都不在则降级为文字输出
try:
    from astrbot.core.utils.html_renderer import html_render_to_pic
except Exception:
    try:
        from astrbot.core.utils.render import html_render_to_pic
    except Exception:
        html_render_to_pic = None

# 交互式会话等待器（/charge_set 区域选择用），v3/v4 导入路径兼容
try:
    from astrbot.core.utils.session_waiter import session_waiter, SessionController
except Exception:
    try:
        from astrbot.api.event.filter import session_waiter  # type: ignore
        SessionController = None
    except Exception:
        session_waiter = None
        SessionController = None

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_device_map_file():
    """模块加载时读一次 device_map.json，用于生成区域快捷触发正则"""
    try:
        with open(os.path.join(_PLUGIN_DIR, "device_map.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[ChargeStationPlugin] 生成区域正则时读取 device_map.json 失败: {e}")
        return {}


def _build_area_regex(device_map):
    """南湖校区区域快捷触发正则，跟随 device_map.json 自动生成，新增区域无需改代码"""
    areas = list(device_map.get("南湖", {}).keys())
    if not areas:
        areas = ["东门", "学院", "矿科", "兰", "桃", "松", "竹", "生活"]
    return "^(" + "|".join(areas) + ")$"


_AREA_REGEX = _build_area_regex(_load_device_map_file())

_STATUS_CSS = """
* { box-sizing: border-box; }
body {
    margin: 0; padding: 16px; background: #eef1f5;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
                 "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
.wrap { width: 960px; }
.campus {
    background: #ffffff; border-radius: 14px; padding: 16px 18px 18px; margin-bottom: 14px;
    box-shadow: 0 1px 4px rgba(15, 23, 42, .08);
}
.campus h2 { margin: 0 0 3px; font-size: 20px; color: #0f172a; }
.sub { font-size: 12px; color: #94a3b8; margin-bottom: 10px; }
.area-label { font-size: 13px; color: #64748b; margin: 14px 0 8px; font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.dev { border: 1px solid #e2e8f0; border-radius: 10px; padding: 9px 10px 10px; background: #fff; }
.dev.missing { border-style: dashed; background: #fafafa; }
.dev h3 {
    margin: 0 0 8px; font-size: 14px; color: #1e293b; font-weight: 600;
    display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
}
.dev h3 .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dev h3 .stat { font-size: 12px; color: #16a34a; font-weight: 700; white-space: nowrap; }
.dev.missing h3 { color: #94a3b8; }
.nodata { font-size: 12px; color: #b0b8c4; text-align: center; padding: 8px 0; }
.ports { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; }
.port {
    display: flex; justify-content: center; align-items: center; gap: 4px;
    border-radius: 6px; padding: 4px 6px; font-size: 12px;
    font-variant-numeric: tabular-nums; min-width: 0; white-space: nowrap;
}
.port .idx { font-weight: 700; }
.port.free { background: #ecfdf5; color: #047857; }
.port.busy { background: #fff7ed; color: #9a3412; }
.foot { color: #94a3b8; font-size: 12px; text-align: center; padding: 4px 0 6px; }
"""


@register("astrbot_plugin_charge_status", "YL1EYE", "查询cumt充电桩端口状态", "1.2.0")
class ChargeStationPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_config = config
        self.data_dir = os.path.dirname(__file__)
        self.device_map_path = os.path.join(self.data_dir, "device_map.json")
        self.device_map = self._load_device_map()
        self.hash_path = os.path.join(self.data_dir, "hash.json")
        self.hash_map = self._load_hash_map()
        self.DEFAULT_SUID = "0"
        self.API_URL = "https://api.powerliber.com/client/1/device/detail"
        self.TOKEN = "token"
        # 全局缓存 (所有用户共用)
        self.cache = {}
        # /report 频率限制: {user_id: last_ts}
        self._report_ts = {}

    def _cfg(self, key, default=None):
        c = getattr(self, "plugin_config", None)
        if c is None:
            return default
        try:
            return c.get(key, default)
        except Exception:
            return default

    @property
    def _use_image(self):
        return bool(self._cfg("use_image", True))

    @property
    def _owner_id(self):
        return str(self._cfg("owner_id", "") or "").strip()

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
        """从 hash.json 读取 device_id -> SUID 映射表"""
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
        """获取校区下的所有区域列表（保持 device_map.json 中的顺序，去重）"""
        areas = []
        target_map = self.device_map

        if campus:
            if campus not in self.device_map:
                return []
            target_map = {campus: self.device_map.get(campus, {})}

        for region, types in target_map.items():
            areas.extend(types.keys())

        return list(dict.fromkeys(areas))

    def _collect_device_ids(self, campus=None, area=None, fuzzy=False):
        """收集需要查询的 device_id 列表"""
        device_ids = []
        if campus:
            campus_map = self.device_map.get(campus, {})
            if fuzzy and area:
                for a_name, devices in campus_map.items():
                    if area in a_name:
                        device_ids.extend(devices.keys())
            else:
                for devices in campus_map.values():
                    device_ids.extend(devices.keys())
        else:
            for types_ in self.device_map.values():
                for devices in types_.values():
                    device_ids.extend(devices.keys())
        return device_ids

    def _format_device_map(self, ports_data=None, campus=None, area=None):
        """格式化输出设备映射表（文字版，作为图片渲染失败时的降级输出）"""
        lines = []
        target_map = self.device_map

        if campus:
            target_map = {campus: self.device_map.get(campus, {})}

        for campus_name, regions in target_map.items():
            lines.append(f"校区：{campus_name}")

            for type_, devices in regions.items():
                if area and type_ != area:
                    continue

                lines.append(f"  区域：{type_} （时间仅供参考）")

                for device_id, device_name in devices.items():
                    ports_info = ""
                    if ports_data:
                        dev_ports = ports_data.get(str(device_id), [])
                        # 未找到 SUID
                        if dev_ports == [] and self.hash_map.get(device_id, self.DEFAULT_SUID) == self.DEFAULT_SUID:
                            ports_info = "⚠ 提供使用记录补充此数据"
                        else:
                            # 按端口索引排序，保证显示顺序稳定
                            dev_ports_sorted = sorted(dev_ports, key=lambda p: p.get("port_index", 0))

                            # 计算端口索引位数，保证对齐（例如 0 -> " 0", 10 -> "10"）
                            max_idx_digits = max((len(str(p.get("port_index", 0))) for p in dev_ports_sorted),
                                                 default=1)

                            # 生成每个端口的短字符串，端口号突出放在方括号内
                            entries = []
                            for p in dev_ports_sorted:
                                idx = p.get("port_index", 0) + 1
                                label = f"[{idx:>{max_idx_digits}}]"  # 右对齐端口数字
                                if p.get("charge_status") == 1:
                                    time_consumed = p.get("time_consumed", 0)
                                    entry = f"{label} {round(time_consumed/60,1):g}h"
                                else:
                                    entry = f"{label} 空闲"
                                entries.append(entry)

                            if not entries:
                                ports_info = "⚠ 无端口数据"
                            else:
                                # 每行显示 4 列
                                cols_per_row = 4
                                row_lines = []
                                for i in range(0, len(entries), cols_per_row):
                                    chunk = entries[i:i + cols_per_row]
                                    row_lines.append("".join(chunk))
                                ports_info = "\n".join(row_lines)

                    # 设备名对齐输出
                    total_width = 32
                    text_w = 0
                    for ch in device_name:
                        if unicodedata.east_asian_width(ch) in ("F", "W"):
                            text_w += 2
                        else:
                            text_w += 1
                    pad = max(total_width - text_w, 0)
                    left = pad // 2
                    right = pad - left
                    centered_name = "-" * left + device_name + "-" * right
                    lines.append(centered_name)

                    if ports_info:
                        for pline in ports_info.splitlines():
                            lines.append(pline)
                # 区域之间空行
                lines.append("")
        return "\n".join(lines)

    # ---------- 图片渲染 ----------

    def _build_status_html(self, ports_data, campus=None, area=None):
        """把端口状态组织成 HTML 卡片页"""
        target_map = self.device_map
        if campus:
            target_map = {campus: self.device_map.get(campus, {})}

        sections = []
        for campus_name, regions in target_map.items():
            area_blocks = []
            for type_, devices in regions.items():
                if area and type_ != area:
                    continue
                cards = []
                for device_id, device_name in devices.items():
                    dev_ports = ports_data.get(str(device_id), []) if ports_data else []
                    has_suid = self.hash_map.get(device_id, self.DEFAULT_SUID) != self.DEFAULT_SUID

                    if dev_ports == [] and not has_suid:
                        cards.append(
                            f'<div class="dev missing"><h3>{_html.escape(device_name)}</h3>'
                            f'<div class="nodata">待补充 SUID</div></div>'
                        )
                        continue
                    if not dev_ports:
                        cards.append(
                            f'<div class="dev missing"><h3>{_html.escape(device_name)}</h3>'
                            f'<div class="nodata">无端口数据</div></div>'
                        )
                        continue

                    dev_ports_sorted = sorted(dev_ports, key=lambda p: p.get("port_index") or 0)
                    free_cnt = sum(1 for p in dev_ports_sorted if p.get("charge_status") != 1)

                    chips = []
                    for p in dev_ports_sorted:
                        idx = (p.get("port_index") or 0) + 1
                        if p.get("charge_status") == 1:
                            t = f"{round((p.get('time_consumed') or 0) / 60, 1):g}"
                            chips.append(
                                f'<div class="port busy"><span class="idx">{idx}</span>'
                                f'<span>{t}h</span></div>'
                            )
                        else:
                            chips.append(
                                f'<div class="port free"><span class="idx">{idx}</span>'
                                f'<span>空闲</span></div>'
                            )

                    stat = f"{free_cnt}/{len(dev_ports_sorted)} 空"
                    cards.append(
                        f'<div class="dev"><h3><span class="nm">{_html.escape(device_name)}</span>'
                        f'<span class="stat">{stat}</span></h3>'
                        f'<div class="ports">{"".join(chips)}</div></div>'
                    )

                area_blocks.append(
                    f'<div class="area-label">{_html.escape(type_)} · 时间仅供参考</div>'
                    f'<div class="grid">{"".join(cards)}</div>'
                )

            sections.append(
                f'<div class="campus"><h2>{_html.escape(campus_name)}</h2>'
                f'<div class="sub">绿色 空闲 · 橙色 充电中（显示已充时长，仅供参考）</div>'
                f'{"".join(area_blocks)}</div>'
            )

        ts = datetime.now().strftime("%H:%M:%S")
        return (
            f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<style>{_STATUS_CSS}</style></head><body><div class="wrap">'
            f'{"".join(sections)}'
            f'<div class="foot">数据时间 {ts} · cumt_charge_status</div>'
            f'</div></body></html>'
        )

    async def _render_status_image(self, ports_data, campus=None, area=None):
        """渲染状态图片，返回 PNG bytes；渲染不可用时返回 None（降级为文字）"""
        if html_render_to_pic is None:
            return None
        try:
            page = self._build_status_html(ports_data, campus=campus, area=area)
            return await html_render_to_pic(page, width=980)
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 图片渲染失败，降级为文字输出: {e}")
            return None

    async def _send_status(self, event: AstrMessageEvent, entry, cached_age=None):
        """统一出口：优先图片，失败降级文字。entry 为缓存条目 dict"""
        if self._use_image:
            img = entry.get("image")
            if img is None:
                img = await self._render_status_image(entry.get("ports_data"))
                entry["image"] = img
            if img:
                prefix = f"(缓存数据，{cached_age}秒前更新)\n" if cached_age is not None else ""
                yield event.chain_result([Comp.Plain(prefix), Comp.Image.fromBytes(img)])
                return
        reply = entry.get("reply", "")
        if cached_age is not None:
            reply = f"(缓存数据，{cached_age}秒前更新)\n{reply}"
        yield event.plain_result(reply)

    async def _fetch_ports_data(self, device_ids):
        """根据 device_id 列表获取端口数据，返回适配 /charge 格式"""
        API_URL = self.API_URL
        TOKEN = self.TOKEN

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
                        if data.get("code") != 0:
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
                                "power": p.get("power", 0),
                                "time_consumed": p.get("time_consumed", 0)
                            } for p in port_list
                        ]

                except Exception as e:
                    logger.error(f"[ChargeStationPlugin] 请求 SUID {suid} 接口失败: {e}")
                    ports_data[str(device_id)] = []
        return {"code": 100000, "data": ports_data}

    @filter.command("charge")
    async def query_charge(self, event: AstrMessageEvent):
        """指令：/charge [校区] [区域]"""
        text = event.get_message_str().strip()
        parts = text.split()
        campus = parts[1] if len(parts) > 1 else None
        area = parts[2] if len(parts) > 2 else None

        # 缓存 key
        cache_key = (campus, area)
        now = time.time()
        entry = self.cache.get(cache_key)
        if entry and now - entry["time"] < 90:
            async for msg in self._send_status(event, entry, cached_age=int(now - entry["time"])):
                yield msg
            return

        device_ids = self._collect_device_ids(campus=campus, area=area)
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
        entry = {"time": now, "ports_data": ports_data, "reply": reply, "image": None}
        self.cache[cache_key] = entry

        async for msg in self._send_status(event, entry):
            yield msg

    @filter.regex(_AREA_REGEX)
    async def query_charge_area(self, event: AstrMessageEvent):
        """只输入区域名时直接查询南湖校区（区域列表自动来自 device_map.json）"""
        area = event.get_message_str().strip()
        # 缓存 key
        cache_key = ("南湖", area)
        now = time.time()
        entry = self.cache.get(cache_key)
        if entry and now - entry["time"] < 60:
            async for msg in self._send_status(event, entry, cached_age=int(now - entry["time"])):
                yield msg
            return

        # 模糊匹配区域名
        device_ids = self._collect_device_ids(campus="南湖", area=area, fuzzy=True)
        if not device_ids:
            yield event.plain_result(f"未找到南湖校区的「{area}」相关设备")
            return

        data = await self._fetch_ports_data(device_ids)
        if not data:
            yield event.plain_result("获取充电桩信息失败")
            return
        if data.get("code") != 100000:
            yield event.plain_result("接口返回错误")
            return

        ports_data = data.get("data", {})
        reply = self._format_device_map(ports_data=ports_data, campus="南湖", area=area)

        entry = {"time": now, "ports_data": ports_data, "reply": reply, "image": None}
        self.cache[cache_key] = entry

        async for msg in self._send_status(event, entry):
            yield msg

    @filter.command("report")
    async def report_to_owner(self, event: AstrMessageEvent):
        """指令：/report <内容> 将用户反馈私聊转发给 owner"""
        owner = self._owner_id
        if not owner:
            yield event.plain_result("⚠️ 插件未配置 owner_id，请在 AstrBot 插件配置里填写后重载")
            return

        text = event.get_message_str().strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法：/report <反馈内容>")
            return
        content = parts[1].strip()[:500]

        uid = event.get_sender_id() or "未知"
        name = event.get_sender_name() or "未知"
        src = "私聊" if event.is_private_chat() else f"群 {event.get_group_id()}"

        # 频率限制：同一用户 60 秒一条
        now = int(time.time())
        if now - self._report_ts.get(uid, 0) < 60:
            yield event.plain_result("⏰ 发送太频繁，请稍后再试")
            return

        report = (
            "📩 收到新反馈\n"
            f"来自：{name} ({uid})\n"
            f"来源：{src}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"内容：{content}"
        )
        try:
            client = event.bot
            await client.api.call_action("send_private_msg", user_id=int(owner), message=report)
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] /report 转发失败: {e}")
            yield event.plain_result(f"❌ 转发失败: {e}")
            return

        self._report_ts[uid] = now
        logger.info(f"[ChargeStationPlugin] 反馈 from {uid}@{src}: {content[:80]}")
        yield event.plain_result("✅ 反馈已送达，感谢")

    @filter.command("whereis")
    async def get_location(self, event: AstrMessageEvent):
        # 经纬度
        lat, lon = "31.2304", "121.4737"
        title = "测试地址"
        content = "上海市 · 人民广场（测试）"
        client = event.bot
        is_private = event.is_private_chat()
        # 构造 CQ 码位置消息
        cq_location = f"[CQ:location,lat={lat},lon={lon},title={title},content={content}]"

        payloads = {
            "message": cq_location
        }

        if is_private:
            payloads["user_id"] = event.get_sender_id()
            await client.api.call_action("send_private_msg", **payloads)
        else:
            payloads["group_id"] = event.get_group_id()
            await client.api.call_action("send_group_msg", **payloads)

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

    # ---------- /charge_set 设备登记 ----------

    def _is_admin(self, event: AstrMessageEvent):
        """owner_id / admin_ids 命中放行；两者都未配置则不限制（自用兼容）"""
        sender = str(event.get_sender_id() or "")
        admins = []
        if self._owner_id:
            admins.append(self._owner_id)
        raw = str(self._cfg("admin_ids", "") or "")
        admins += [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        if not admins:
            return True
        return sender in admins

    def _find_device_location(self, device_id):
        """返回设备当前所在 (校区, 区域)，不在 device_map 中返回 None"""
        for c, regions in self.device_map.items():
            for a, devices in regions.items():
                if str(device_id) in devices:
                    return c, a
        return None

    @staticmethod
    def _parse_choice(text, upper):
        """把回复解析成 1..upper 的序号，不合法返回 None"""
        s = text.strip()
        if s.isdigit() and 1 <= int(s) <= upper:
            return int(s)
        return None

    @staticmethod
    def _valid_name(s, max_len=12):
        return bool(s) and len(s) <= max_len and "/" not in s and "\n" not in s

    def _area_menu(self, state):
        campus = state["campus"]
        areas = list(self.device_map.get(campus, {}).keys())
        state["areas"] = areas
        lines = [f"「{campus}」的区域，回复序号选择："]
        lines += [f"  {i + 1}. {a}" for i, a in enumerate(areas)]
        lines.append("  0. ➕ 新建区域")
        return "\n".join(lines)

    def _name_prompt(self, state):
        """第 3 步:输入显示名。回复自定义名直接写入,回复 y 用 API 默认站名"""
        lines = [
            f"位置：{state['campus']} / {state['area']}",
            f"默认设备名：{state['station']}",
        ]
        if state.get("existing"):
            c, a = state["existing"]
            lines.append(f"⚠ 该设备当前位于 {c}/{a}，将被移动")
        lines.append("回复自定义显示名，或 y 使用默认名（回复后直接写入）")
        return "\n".join(lines)

    def _persist_device(self, suid, device_id, campus, area, station):
        """同时写入 hash.json 和 device_map.json，重载内存并清缓存。失败返回错误信息"""
        try:
            device_id = str(device_id)
            # hash.json
            self.hash_map[device_id] = suid
            with open(self.hash_path, "w", encoding="utf-8") as f:
                json.dump(self.hash_map, f, ensure_ascii=False, indent=4)
            # device_map.json：先移除旧位置（支持移动设备），再写入新位置
            for regions in self.device_map.values():
                for devices in regions.values():
                    devices.pop(device_id, None)
            self.device_map.setdefault(campus, {}).setdefault(area, {})[device_id] = station
            with open(self.device_map_path, "w", encoding="utf-8") as f:
                json.dump(self.device_map, f, ensure_ascii=False, indent=2)
            # 重载内存 + 清缓存，立即生效
            self.hash_map = self._load_hash_map()
            self.device_map = self._load_device_map()
            self.cache.clear()
            return None
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 写入配置失败: {e}")
            return str(e)

    def _dialog_step(self, state, text):
        """处理 /charge_set 交互的一步，返回 (replies, done)"""
        s = (text or "").strip()

        if s.lower() in ("q", "quit", "取消"):
            return ["已取消"], True

        step = state["step"]

        if step == "campus":
            n = self._parse_choice(s, len(state["campus_list"]) + 1)
            if n is None:
                return ["请回复序号选择校区（或 q 取消）"], False
            if n <= len(state["campus_list"]):
                state["campus"] = state["campus_list"][n - 1]
            else:
                state["step"] = "new_campus"
                return ["请输入新校区名称"], False
            state["step"] = "area"
            return [self._area_menu(state)], False

        if step == "new_campus":
            if not self._valid_name(s):
                return ["名称不合法（1-12字，不能含 /）"], False
            state["campus"] = s
            state["new_campus"] = True
            state["step"] = "new_area"
            state["areas"] = []
            return ["新校区还没有区域，请输入区域名称"], False

        if step == "area":
            if s == "0":
                state["step"] = "new_area"
                return ["请输入新区域名称"], False
            n = self._parse_choice(s, len(state["areas"]))
            if n is None:
                return ["请回复序号选择区域（0 新建，q 取消）"], False
            state["area"] = state["areas"][n - 1]
            state["step"] = "name"
            return [self._name_prompt(state)], False

        if step == "new_area":
            if not self._valid_name(s):
                return ["名称不合法（1-12字，不能含 /）"], False
            state["area"] = s
            state["new_area"] = True
            state["step"] = "name"
            return [self._name_prompt(state)], False

        if step == "name":
            s = s.strip()
            if s.lower() in ("y", "yes"):
                name = state["station"]
            elif not self._valid_name(s, 16):
                return ["名称不合法（1-16字，不能含 /），或回复 y 用默认名"], False
            else:
                name = s
            err = self._persist_device(
                state["suid"], state["device_id"], state["campus"], state["area"], name
            )
            if err:
                return [f"❌ 写入失败: {err}"], True
            extra = ""
            if state["campus"] == "南湖" and state.get("new_area"):
                extra = "\n提示：新区域的单词快捷触发需在面板重载插件后生效"
            return [f"✅ 已将设备 {state['device_id']}（{name}）加入 {state['campus']}/{state['area']}{extra}"], True

        return [], True

    @filter.command("charge_set")
    async def set_suid(self, event: AstrMessageEvent):
        """
        指令：/charge_set <suid> [校区/区域 自定义名]
        交互流程共 3 步：选校区 -> 选区域 -> 输入显示名（y 用默认）
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 该指令仅限管理员使用")
            return

        text = event.get_message_str().strip()
        parts = text.split(maxsplit=3)

        # 一步到位：/charge_set <suid> 南湖/杏 [自定义名]
        if len(parts) >= 3:
            suid, loc = parts[1], parts[2]
            custom = parts[3].strip() if len(parts) > 3 else ""
            if "/" not in loc:
                yield event.plain_result("格式：/charge_set <suid> <校区>/<区域> [自定义名]\n示例：/charge_set 401011062900028 南湖/杏 杏1东侧")
                return
            campus, area = [x.strip() for x in loc.split("/", 1)]
            try:
                message, device_id, station = await self.get_device(suid)
            except Exception as e:
                yield event.plain_result(f"请求失败: {e}")
                return
            device_id = str(device_id)
            if custom and not self._valid_name(custom, 16):
                yield event.plain_result("⚠️ 自定义名不合法（1-16字，不能含 /）")
                return
            name = custom or station
            err = self._persist_device(suid, device_id, campus, area, name)
            if err:
                yield event.plain_result(f"❌ 写入失败: {err}")
            else:
                yield event.plain_result(f"✅ 已将设备 {device_id}（{name}）加入 {campus}/{area}")
            return

        if len(parts) != 2:
            yield event.plain_result("用法：/charge_set <suid> [校区/区域 自定义名]\n示例：/charge_set 401011062900028\n省略位置参数时进入交互式选择")
            return

        suid = parts[1]
        try:
            message, device_id, station = await self.get_device(suid)
        except Exception as e:
            yield event.plain_result(f"请求失败: {e}")
            return
        device_id = str(device_id)
        existing = self._find_device_location(device_id)

        if session_waiter is None:
            yield event.plain_result(
                f"{message}\n当前 AstrBot 版本不支持交互选择，请补上位置参数：\n/charge_set <suid> <校区>/<区域>"
            )
            return

        state = {
            "step": "campus",
            "suid": suid,
            "device_id": device_id,
            "station": station,
            "existing": existing,
            "campus_list": list(self.device_map.keys()),
        }

        menu_lines = [message, "请回复序号选择校区（120秒内有效，q 取消）："]
        menu_lines += [f"  {i + 1}. {c}" for i, c in enumerate(state["campus_list"])]
        menu_lines.append(f"  {len(state['campus_list']) + 1}. ➕ 新建校区")
        yield event.plain_result("\n".join(menu_lines))

        sender_id = event.get_sender_id()

        @session_waiter(timeout=120)
        async def controller(ctrl, ev: AstrMessageEvent):
            # 群里只认发起人的回复
            if ev.get_sender_id() != sender_id:
                return
            replies, done = self._dialog_step(state, ev.get_message_str())
            for r in replies:
                yield ev.plain_result(r)
            if done:
                await ctrl.stop_propagation()

        await controller(event.unified_msg_origin)

    async def get_device(self, SUID):
        API_URL = self.API_URL
        TOKEN = self.TOKEN
        async with aiohttp.ClientSession() as session:
            payload = {
                "token": TOKEN,
                "client_id": 1,
                "app_id": "dd",
                "suid": SUID
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
            }
            async with session.post(API_URL, data=payload, headers=headers) as resp:
                data = await resp.json()
                device = data["data"]["device"]
                result = f"设备ID: {device['id']} 位置:{device['station_name']}(端口数: {device['port_count']})"
        return result, device['id'], device.get('station_name') or f"设备{device['id']}"

    @filter.command("charge_help")
    async def charge_help(self, event: AstrMessageEvent):
        """显示电桩指令帮助信息"""
        help_msg = (
            "充电桩查询指令使用说明：\n"
            "/charge                  显示所有校区所有端口\n"
            "/charge <校区>            显示指定校区所有端口\n"
            "/charge <校区> <区域>      显示指定校区指定区域端口\n"
            "直接发送区域名             查询南湖校区该区域\n"
            "/charge_list <校区>       显示指定校区区域列表\n"
            "/charge_refresh          强制清空缓存，下次查询获取最新数据\n"
            "/report <内容>            向维护者发送反馈\n"
            "/charge_set <suid> [位置]  管理员绑定新设备（交互选择区域）\n"
            "/charge_help             显示帮助信息\n"
        )
        yield event.plain_result(help_msg)

    async def initialize(self):
        logger.info("[ChargeStationPlugin] 插件已初始化")

    async def terminate(self):
        logger.info("[ChargeStationPlugin] 插件已卸载")
