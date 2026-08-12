"""Просмотр вызовов скиллов на /ui: линейный график (Chart.js) по дням, один скилл — одна линия."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

import config
from config import log

router = APIRouter()

# Палитра — references/palette.md из скилла dataviz. 7 слотов для самых используемых
# скиллов (по общему числу вызовов за всё время, а не по текущему фильтру — иначе при
# смене фильтра выжившие линии перекрашивались бы, см. anti-patterns.md "Recolor-on-filter").
# Всё, что не попало в топ-7, идёт в "Другое" отдельным нейтральным цветом.
FEATURED_SLOTS = 7
DEFAULT_WINDOW_DAYS = 30
MAX_CHART_DAYS = 400
OTHER_LABEL = "Другое"

GLOBAL_RANKING_SQL = "SELECT skill, count(*) AS n FROM skill_usage GROUP BY skill ORDER BY n DESC, skill ASC"


def day_range(start: datetime, end: datetime) -> list[datetime]:
    """Список полуночей UTC от start до end включительно — ось X графика."""
    start_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    d = start_day
    while d <= end_day:
        days.append(d)
        d += timedelta(days=1)
    return days


def parse_day_param(value: str, *, end_of_day: bool) -> datetime | None:
    """Разбирает значение <input type="date"> (YYYY-MM-DD) в границу суток UTC."""
    if not value:
        return None
    try:
        day = datetime.fromisoformat(value)
    except ValueError:
        return None
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.replace(tzinfo=timezone.utc)


def embed_json(obj: Any) -> str:
    """JSON для вставки в <script>: экранирует "</", иначе строка вида skill.name="</script>"
    из данных (пришли по сети, не наши) обрывает тег раньше времени."""
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


async def load_chart_data(conn, conditions: list[str], params: list[Any], since_day: datetime, until_day: datetime):
    """Возвращает данные для Chart.js: ось X — дни, датасет на каждый из топ-N скиллов + "Другое"."""
    ranking = await conn.fetch(GLOBAL_RANKING_SQL)
    featured = [r["skill"] for r in ranking[:FEATURED_SLOTS]]
    slot_of = {name: f"--series-{i + 1}" for i, name in enumerate(featured)}

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = (
        "SELECT date_trunc('day', occurred_at) AS bucket, skill, count(*)::int AS n "
        f"FROM skill_usage {where} GROUP BY bucket, skill ORDER BY bucket"
    )
    rows = await conn.fetch(sql, *params)

    days = day_range(since_day, until_day)
    day_index = {d.date().isoformat(): i for i, d in enumerate(days)}

    series: dict[str, list[int]] = {}
    for r in rows:
        idx = day_index.get(r["bucket"].date().isoformat())
        if idx is None:
            continue  # вне диапазона дней — не должно происходить при согласованных условиях, но не рушим страницу
        label = r["skill"] if r["skill"] in slot_of else OTHER_LABEL
        series.setdefault(label, [0] * len(days))[idx] += r["n"]

    ordered_labels = [s for s in featured if s in series]
    if OTHER_LABEL in series:
        ordered_labels.append(OTHER_LABEL)

    datasets = [
        {"label": label, "color_var": slot_of.get(label, "--series-other"), "data": series[label]}
        for label in ordered_labels
    ]

    return {"labels": [d.date().isoformat() for d in days], "datasets": datasets}


UI_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>cc-metrics</title>
<style>
  .viz-root {{
    color-scheme: light;
    --page: #f9f9f7; --surface-1: #fcfcfb;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --gridline: #e1e0d9; --baseline: #c3c2b7;
    --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
    --series-5: #e87ba4; --series-6: #008300; --series-7: #4a3aa7; --series-other: #898781;
  }}
  @media (prefers-color-scheme: dark) {{
    .viz-root {{
      color-scheme: dark;
      --page: #0d0d0d; --surface-1: #1a1a19;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --gridline: #2c2c2a; --baseline: #383835;
      --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
      --series-5: #d55181; --series-6: #008300; --series-7: #9085e9; --series-other: #898781;
    }}
  }}
  .viz-root {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; padding: 2rem; background: var(--page); color: var(--text-primary);
  }}
  h1 {{ font-size: 1.2rem; margin: 0 0 1rem; }}
  form {{ display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: end; margin-bottom: 1rem; }}
  label {{ display: flex; flex-direction: column; font-size: 0.75rem; color: var(--text-secondary); }}
  input {{
    padding: 0.35rem; font-size: 0.9rem; border: 1px solid var(--baseline); border-radius: 4px;
    background: var(--surface-1); color: var(--text-primary);
  }}
  button {{
    padding: 0.4rem 0.9rem; cursor: pointer; border: 1px solid var(--baseline); border-radius: 4px;
    background: var(--surface-1); color: var(--text-primary);
  }}
  .muted {{ color: var(--text-muted); font-size: 0.85rem; }}
  .error {{ color: #b00020; }}
  .chart-card {{ background: var(--surface-1); border: 1px solid var(--gridline); border-radius: 8px; padding: 1rem; }}
  .chart-note {{ color: var(--text-muted); font-size: 0.78rem; margin-top: 0.6rem; }}
  .cc-tooltip {{
    position: absolute; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--surface-1); border: 1px solid var(--gridline); border-radius: 6px;
    padding: 0.5rem 0.65rem; font-size: 0.8rem; box-shadow: 0 2px 8px rgba(0,0,0,.15); z-index: 10;
  }}
  .cc-tooltip-header {{ color: var(--text-muted); font-size: 0.72rem; margin-bottom: 0.3rem; }}
  .cc-tooltip-row {{ display: flex; align-items: center; gap: 0.4rem; white-space: nowrap; }}
  .cc-tooltip-key {{ width: 10px; height: 2px; border-radius: 1px; flex: 0 0 auto; }}
  .cc-tooltip-row strong {{ color: var(--text-primary); font-variant-numeric: tabular-nums; }}
  .cc-tooltip-name {{ color: var(--text-secondary); }}
</style></head>
<body class="viz-root">
<h1>Вызовы скиллов Claude Code</h1>
<form method="get" action="/ui">
  <label>Пользователь<input type="text" name="user" value="{user}" placeholder="vasya@01.tech"></label>
  <label>Скилл<input type="text" name="skill" value="{skill}" placeholder="cr"></label>
  <label>С<input type="date" name="since" value="{since}"></label>
  <label>По<input type="date" name="until" value="{until}"></label>
  <button type="submit">Фильтровать</button>
</form>
{body}
</body></html>"""


def render_ui_page(
    *,
    error: str = "",
    chart: dict[str, Any] | None = None,
    clamped: bool = False,
    filters: dict[str, str],
) -> str:
    if error:
        body = f'<p class="error">{escape(error)}</p>'
    elif not chart or not chart["datasets"]:
        body = '<p class="muted">Ничего не найдено.</p>'
    else:
        note_parts = []
        if clamped:
            note_parts.append(f"диапазон ограничен {MAX_CHART_DAYS} днями")
        if any(ds["label"] == OTHER_LABEL for ds in chart["datasets"]):
            note_parts.append(
                f'«{OTHER_LABEL}» объединяет скиллы за пределами топ-{FEATURED_SLOTS} по общему числу вызовов'
            )
        note = f'<p class="chart-note">{escape("; ".join(note_parts))}.</p>' if note_parts else ""

        body = f"""
<div class="chart-card">
  <div style="position:relative;height:420px;">
    <canvas id="cc-chart" role="img" aria-label="Вызовы скиллов по дням"></canvas>
  </div>
</div>
{note}
<div id="cc-tooltip" class="cc-tooltip"></div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.min.js"
        integrity="sha384-XcdcwHqIPULERb2yDEM4R0XaQKU3YnDsrTmjACBZyfdVVqjh6xQ4/DCMd7XLcA6Y"
        crossorigin="anonymous"></script>
<script>
(function () {{
  const DATA = {embed_json(chart)};
  const root = document.querySelector('.viz-root');
  function cssVar(name) {{ return getComputedStyle(root).getPropertyValue(name).trim(); }}
  function tokens() {{
    return {{
      surface: cssVar('--surface-1'),
      textSecondary: cssVar('--text-secondary'),
      textMuted: cssVar('--text-muted'),
      gridline: cssVar('--gridline'),
      baseline: cssVar('--baseline'),
    }};
  }}

  const dayLabels = DATA.labels.map(function (iso) {{
    return new Date(iso + 'T00:00:00').toLocaleDateString('ru-RU', {{day: 'numeric', month: 'short'}});
  }});

  const tooltipEl = document.getElementById('cc-tooltip');

  function externalTooltip(context) {{
    const tooltip = context.tooltip;
    if (!tooltip || tooltip.opacity === 0) {{ tooltipEl.style.opacity = 0; return; }}
    const points = tooltip.dataPoints || [];
    const header = document.createElement('div');
    header.className = 'cc-tooltip-header';
    header.textContent = points.length ? points[0].label : '';
    const rows = document.createElement('div');
    points.forEach(function (p) {{
      const row = document.createElement('div');
      row.className = 'cc-tooltip-row';
      const key = document.createElement('span');
      key.className = 'cc-tooltip-key';
      key.style.background = p.dataset.borderColor;
      const value = document.createElement('strong');
      value.textContent = Number(p.raw).toLocaleString('ru-RU');
      const name = document.createElement('span');
      name.className = 'cc-tooltip-name';
      name.textContent = p.dataset.label;
      row.appendChild(key); row.appendChild(value); row.appendChild(name);
      rows.appendChild(row);
    }});
    tooltipEl.replaceChildren(header, rows);
    const rect = context.chart.canvas.getBoundingClientRect();
    tooltipEl.style.opacity = 1;

    // У правого края (последний день на графике) тултип справа от курсора не влезает
    // в экран — переносим его влево. offsetWidth читаем после replaceChildren, чтобы
    // мерить актуальную ширину, а не ширину предыдущей наведённой точки.
    let left = rect.left + window.scrollX + tooltip.caretX + 12;
    const overflowsRight = left + tooltipEl.offsetWidth > window.scrollX + document.documentElement.clientWidth - 8;
    if (overflowsRight) {{
      left = rect.left + window.scrollX + tooltip.caretX - tooltipEl.offsetWidth - 12;
    }}
    tooltipEl.style.left = left + 'px';
    tooltipEl.style.top = (rect.top + window.scrollY + tooltip.caretY) + 'px';
  }}

  const crosshair = {{
    id: 'crosshair',
    afterDraw: function (chart) {{
      const active = chart.getActiveElements();
      if (!active.length) return;
      const x = active[0].element.x;
      const area = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = tokens().gridline;
      ctx.stroke();
      ctx.restore();
    }}
  }};

  let chart = null;

  function build() {{
    if (chart) chart.destroy();
    const t = tokens();
    const ctx = document.getElementById('cc-chart').getContext('2d');
    chart = new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: dayLabels,
        datasets: DATA.datasets.map(function (ds) {{
          const color = cssVar(ds.color_var);
          return {{
            label: ds.label, data: ds.data,
            borderColor: color, backgroundColor: color, borderWidth: 2,
            pointStyle: 'line', pointRadius: 0, pointHoverRadius: 5,
            pointHoverBorderWidth: 2, pointHoverBorderColor: t.surface,
            tension: 0,
          }};
        }}),
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        interaction: {{mode: 'index', intersect: false}},
        plugins: {{
          legend: {{
            position: 'top', align: 'start',
            labels: {{usePointStyle: true, color: t.textSecondary}},
          }},
          tooltip: {{enabled: false, external: externalTooltip}},
        }},
        scales: {{
          x: {{
            grid: {{display: false}},
            ticks: {{color: t.textMuted, autoSkip: true, maxRotation: 0}},
            border: {{color: t.baseline}},
          }},
          y: {{
            beginAtZero: true,
            grid: {{color: t.gridline}},
            ticks: {{
              color: t.textMuted, precision: 0,
              callback: function (v) {{ return Number(v).toLocaleString('ru-RU'); }},
            }},
            border: {{display: false}},
          }},
        }},
      }},
      plugins: [crosshair],
    }});
  }}

  build();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', build);
}})();
</script>
"""

    return UI_PAGE_TEMPLATE.format(
        user=escape(filters["user"]),
        skill=escape(filters["skill"]),
        since=escape(filters["since"]),
        until=escape(filters["until"]),
        body=body,
    )


@router.get("/ui", response_class=HTMLResponse, dependencies=[Depends(config.require_ui_auth)])
async def ui(
    user: str = Query(""),
    skill: str = Query(""),
    since: str = Query(""),
    until: str = Query(""),
) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    until_dt = parse_day_param(until, end_of_day=True) or now
    since_dt = parse_day_param(since, end_of_day=False)
    if since_dt is None:
        since_dt = until_dt - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
        since_display = since_dt.date().isoformat()
    else:
        since_display = since

    clamped = False
    if (until_dt.date() - since_dt.date()).days > MAX_CHART_DAYS:
        since_dt = until_dt - timedelta(days=MAX_CHART_DAYS)
        since_display = since_dt.date().isoformat()
        clamped = True

    filters = {"user": user, "skill": skill, "since": since_display, "until": until}

    if config.pool is None:
        return HTMLResponse(
            render_ui_page(error="DATABASE_URL не задан — приёмник пишет только в лог, смотреть нечего.",
                            filters=filters),
            status_code=503,
        )

    conditions = []
    params: list[Any] = []
    if user:
        params.append(f"%{user}%")
        conditions.append(f"user_email ILIKE ${len(params)}")
    if skill:
        params.append(f"%{skill}%")
        conditions.append(f"skill ILIKE ${len(params)}")
    params.append(since_dt)
    conditions.append(f"occurred_at >= ${len(params)}")
    params.append(until_dt)
    conditions.append(f"occurred_at <= ${len(params)}")

    try:
        async with config.pool.acquire() as conn:
            chart = await load_chart_data(conn, conditions, params, since_dt, until_dt)
    except Exception:
        log.exception("не смог прочитать skill_usage для /ui")
        return HTMLResponse(render_ui_page(error="Не смог прочитать данные из базы.", filters=filters),
                             status_code=503)

    return HTMLResponse(render_ui_page(chart=chart, clamped=clamped, filters=filters))
