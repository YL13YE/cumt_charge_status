from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import aiohttp


@register("astrbot_plugin_charge", "YourName", "查询学校充电桩情况", "1.0.2")
class ChargeStationPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config

        # 分层设备映射（南湖校区示例）
        self.DEVICE_MAP = {
            "南湖": {
                "学院": {
                    609529: "计算机学院（外）",
                    346571: "人文学院西（北）",
                    277329: "机电学院南",
                    503351: "材料与物理学院",
                    276577: "人文学院西（南）",
                    225543: "行健楼东南侧",
                    240736: "化工学院南",
                    367828: "环测学院东（北）",
                    459776: "机电学院2",
                    459775: "机电学院1",
                    609528: "计算机学院（内）",
                    277152: "信控学院北门",
                },
                "宿舍": {
                    # TODO: 补充宿舍区 ID→名称
                },
                "教学": {
                    # TODO: 补充教学区 ID→名称
                },
                "生活": {
                    # TODO: 补充生活区 ID→名称
                },
                "停车场": {
                    # TODO: 补充停车场 ID→名称
                },
            }
        }

    async def initialize(self):
        logger.info("[ChargeStationPlugin] 插件已初始化")

    @filter.command("电桩")
    async def query_charge(self, event: AstrMessageEvent):
        """查询电桩情况，用法：/电桩 南湖 学院"""
        text = event.get_message_str().strip()
        parts = text.split()

        if len(parts) < 3:
            campuses = "、".join(self.DEVICE_MAP.keys())
            reply = f"用法：/电桩 <校区> <区域>\n可选校区：{campuses}"
            yield event.plain_result(reply)
            return

        campus, area = parts[1], parts[2]

        if campus not in self.DEVICE_MAP:
            campuses = "、".join(self.DEVICE_MAP.keys())
            yield event.plain_result(f"未知校区：{campus}\n可选校区：{campuses}")
            return

        if area not in self.DEVICE_MAP[campus]:
            areas = "、".join(self.DEVICE_MAP[campus].keys())
            yield event.plain_result(f"未知区域：{area}\n{campus} 可选区域：{areas}")
            return

        devices = self.DEVICE_MAP[campus][area]
        if not devices:
            yield event.plain_result(f"{campus}-{area} 暂无设备信息")
            return

        device_ids = list(devices.keys())
        ids_str = ",".join(str(x) for x in device_ids)
        url = f"https://lwstools.xyz/api/charge_station/ports?device_ids={ids_str}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()
        except Exception as e:
            logger.error(f"[ChargeStationPlugin] 获取数据失败: {e}")
            yield event.plain_result("查询失败，请稍后再试。")
            return

        if data.get("code") != 100000:
            yield event.plain_result("接口返回错误。")
            return

        ports_data = data.get("data", {})
        total_ports = sum(len(ports_data.get(str(dev), [])) for dev in device_ids)

        details = []
        for dev, name in devices.items():
            free_count = len(ports_data.get(str(dev), []))
            details.append(f"{name}: {free_count} 个空闲")

        reply = f"[{campus}-{area}] 可用插口总数：{total_ports}\n" + "\n".join(details)
        yield event.plain_result(reply)

    async def terminate(self):
        logger.info("[ChargeStationPlugin] 插件已卸载")
