"""充电桩状态图片渲染模块

从 main.py 分离：负责把 ports_data 组织成 HTML 卡片页并渲染成 PNG。
渲染环境不可用（无 chromium / AstrBot 版本差异）时返回 None，由调用方降级为文字。
"""
import html as _html
from datetime import datetime

from astrbot.api import logger

# AstrBot v3 / v4 的 HTML 渲染入口路径不同，逐个尝试
try:
    from astrbot.core.utils.html_renderer import html_render_to_pic
except Exception:
    try:
        from astrbot.core.utils.render import html_render_to_pic
    except Exception:
        html_render_to_pic = None

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


def build_status_html(device_map, hash_map, ports_data, campus=None, area=None, default_suid="0"):
    """把端口状态组织成 HTML 卡片页"""
    target_map = device_map
    if campus:
        target_map = {campus: device_map.get(campus, {})}

    sections = []
    for campus_name, regions in target_map.items():
        area_blocks = []
        for type_, devices in regions.items():
            if area and type_ != area:
                continue
            cards = []
            for device_id, device_name in devices.items():
                dev_ports = ports_data.get(str(device_id), []) if ports_data else []
                has_suid = hash_map.get(device_id, default_suid) != default_suid

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


async def render_status_image(device_map, hash_map, ports_data, campus=None, area=None, default_suid="0"):
    """渲染状态图片，返回 PNG bytes；环境不可用时返回 None（调用方降级为文字）"""
    if html_render_to_pic is None:
        logger.warning("[ChargeStationPlugin] 当前 AstrBot 无 HTML 渲染能力，使用文字输出")
        return None
    try:
        page = build_status_html(device_map, hash_map, ports_data, campus=campus, area=area, default_suid=default_suid)
        return await html_render_to_pic(page, width=980)
    except Exception as e:
        logger.error(f"[ChargeStationPlugin] 图片渲染失败，降级为文字输出: {e}")
        return None
