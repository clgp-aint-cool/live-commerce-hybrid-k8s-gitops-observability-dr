#!/usr/bin/env python3
"""Sinh dashboard + alert rule Grafana cho Live Commerce trên cluster CMC HCM-01.

Chạy:  python3 platform/monitoring/grafana/gen.py
Ra:    dashboards/*.json, alerting/*.yaml (commit cả file sinh ra).

Nhãn metric của Prometheus CMC (ns cmc-monitoring) KHÁC kube-prometheus-stack:
  - node-exporter : node = label `node_name`; `instance` là IP pod (10.100.x.x:9100), KHÔNG dùng được.
                    Không có node_uname_info, node_pressure_*.
  - cadvisor      : node = `node_name` (instance cũng là tên node). Network chỉ có ở series của
                    pause container (không có label `container`), có cả interface tunl0 (Calico) → lọc eth0.
                    CPU có series cấp pod (container="") → luôn lọc container!="".
  - kube-state-metrics (v2): node = label `node`. Không có kube_node_labels → nodegroup suy ra từ tên node.
  - job apiservers có 1 target rác (10.100.x.x:80, luôn down) → chỉ lấy instance=~".*:6443".
Log: Loki (promtail) có label namespace, app, container, pod, node_name, job="<ns>/<app>".
Kong (ns kong, container proxy) log định dạng combined, KHÔNG có latency cho tới khi bật log format
`lc_combined` trong platform/kong/values.yaml (thêm rt=<giây>).
"""
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent
PROM = {"type": "prometheus", "uid": "${ds_prom}"}
LOKI = {"type": "loki", "uid": "${ds_loki}"}
# UID cố định khai báo trong values.yaml (datasources) để alert rule tham chiếu được.
PROM_UID, LOKI_UID = "prometheus", "loki"

APP_NS = "live-commerce-dev|moderation-dev"
NODEGROUP = (
    'label_replace(label_replace({m}, "nodegroup", "$1", "{l}", ".*nodegroup-([a-z0-9]+)-worker.*"),'
    ' "nodegroup", "control-plane", "{l}", ".*control-plane.*")'
)
# Pod -> deployment: <deploy>-<pod-template-hash>-<5 ký tự>
POD_DEPLOY = 'label_replace({m}, "deployment", "$1", "pod", "(.+)-[a-z0-9]{{8,10}}-[a-z0-9]{{5}}")'

# ---- Kong access log -> label svc (theo Ingress kong-routes) ----
KONG_SEL = '{namespace="kong", container="proxy"} != "kong-ingress-controller"'
KONG_PARSE = (
    r'| regexp `^\S+ \S+ \S+ \[[^\]]+\] "(?P<method>[A-Z]+) (?P<path>[^ ?"]*)[^"]*" (?P<status>\d{3}) `'
    ' | status != ""'
    ' | label_format svc=`{{ if hasPrefix "/api/products" .path }}product-service'
    '{{ else if hasPrefix "/api/auth" .path }}auth-service'
    '{{ else if hasPrefix "/api/orders" .path }}order-service'
    '{{ else if hasPrefix "/api/sellers" .path }}live-manager'
    '{{ else if hasPrefix "/api/sessions" .path }}live-manager'
    '{{ else if hasPrefix "/api/lives" .path }}live-manager'
    '{{ else if hasPrefix "/ws" .path }}chat-service'
    '{{ else if hasPrefix "/socket.io" .path }}chat-service'
    '{{ else }}web{{ end }}`'
)
# Gom path có id: /api/lives/<uuid> -> /api/lives/:id (chỉ dùng cho dashboard; rule không cần path).
PATH_NORM = (
    ' | label_format path=`{{ regexReplaceAll "/[0-9]+" (regexReplaceAll'
    ' "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" .path ":id") "/:id" }}`'
)
KONG_RULE = f"{KONG_SEL} {KONG_PARSE}"
KONG = KONG_RULE + PATH_NORM
KONG_RT = f'{KONG} | regexp `rt=(?P<rt>[0-9.]+)` | unwrap rt'


# =====================================================================================
# Panel helpers
# =====================================================================================
class Grid:
    def __init__(self):
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.panels = []
        self.next_id = 1

    def row(self, title, collapsed=False):
        self._newline()
        p = {"type": "row", "title": title, "collapsed": collapsed, "panels": [],
             "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "id": self._id()}
        self.panels.append(p)
        self.y += 1
        return p

    def add(self, panel, w, h):
        if self.x + w > 24:
            self._newline()
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)
        return panel

    def _newline(self):
        self.y += self.row_h
        self.x = 0
        self.row_h = 0

    def _id(self):
        self.next_id += 1
        return self.next_id - 1


def tgt(expr, legend="", ds=PROM, instant=False, ref="A", fmt=None):
    t = {"datasource": ds, "expr": expr, "refId": ref, "legendFormat": legend or "__auto"}
    if ds is PROM:
        t["range"] = not instant
        t["instant"] = instant
    else:
        t["queryType"] = "instant" if instant else "range"
    if fmt:
        t["format"] = fmt
    return t


def thresholds(*steps):
    """steps: (value, color) – value None = base."""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


GREEN_RED = thresholds((None, "green"), (80, "orange"), (90, "red"))


def stat(title, targets, unit="short", th=None, desc="", decimals=None, color_mode="background",
         text_mode="auto", mappings=None, graph="none", ds=PROM, links=None):
    p = {
        "type": "stat", "title": title, "description": desc, "datasource": ds, "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": th or thresholds((None, "green")),
                                     "color": {"mode": "thresholds"}, "mappings": mappings or []},
                        "overrides": []},
        "options": {"colorMode": color_mode, "graphMode": graph, "justifyMode": "center",
                    "textMode": text_mode, "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "auto", "showPercentChange": False, "wideLayout": True},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if links:
        p["links"] = links
    return p


def ts(title, targets, unit="short", desc="", stack=False, th=None, min_=None, max_=None, ds=PROM,
       draw="line", overrides=None, legend_calcs=("mean", "max", "lastNotNull"), th_style="off", fill=10):
    custom = {"drawStyle": draw, "lineWidth": 1, "fillOpacity": fill, "showPoints": "never",
              "spanNulls": False, "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "thresholdsStyle": {"mode": th_style}, "axisSoftMin": 0}
    d = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
         "thresholds": th or thresholds((None, "green"))}
    if min_ is not None:
        d["min"] = min_
    if max_ is not None:
        d["max"] = max_
    return {
        "type": "timeseries", "title": title, "description": desc, "datasource": ds, "targets": targets,
        "fieldConfig": {"defaults": d, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": list(legend_calcs),
                               "sortBy": "Max", "sortDesc": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def table(title, targets, desc="", overrides=None, transformations=None, ds=PROM, sort=None):
    return {
        "type": "table", "title": title, "description": desc, "datasource": ds, "targets": targets,
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                                "filterable": True},
                                     "thresholds": thresholds((None, "green"))},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": sort or [],
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "transformations": transformations or [],
    }


def bargauge(title, targets, unit="percent", max_=100, th=None, desc=""):
    return {
        "type": "bargauge", "title": title, "description": desc, "datasource": PROM, "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": max_, "thresholds": th or GREEN_RED,
                                     "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                    "valueMode": "color", "namePlacement": "left", "sizing": "auto",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    }


def logs(title, expr, desc=""):
    return {"type": "logs", "title": title, "description": desc, "datasource": LOKI,
            "targets": [tgt(expr, ds=LOKI)],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False}}


def text(content, title=""):
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


def unit_override(match, unit=None, color=None, prop_extra=None):
    props = []
    if unit:
        props.append({"id": "unit", "value": unit})
    if color:
        props.append({"id": "color", "value": {"mode": "fixed", "fixedColor": color}})
    props += prop_extra or []
    return {"matcher": {"id": "byName", "options": match}, "properties": props}


def cell_color(match, unit, th, mode="color-background"):
    return {"matcher": {"id": "byName", "options": match},
            "properties": [{"id": "unit", "value": unit}, {"id": "thresholds", "value": th},
                           {"id": "custom.cellOptions", "value": {"type": mode}}]}


# =====================================================================================
# Variables
# =====================================================================================
def v_ds(name, type_, label):
    return {"name": name, "label": label, "type": "datasource", "query": type_, "hide": 0,
            "current": {}, "refresh": 1, "regex": "", "options": []}


def v_query(name, label, query, ds=PROM, multi=True, include_all=True, all_value=".+", regex="",
            hide=0, sort=1, current_all=True):
    v = {"name": name, "label": label, "type": "query", "datasource": ds,
         "query": {"query": query, "refId": f"{name}-var"} if ds is PROM else query,
         "definition": query if isinstance(query, str) else "",
         "refresh": 2, "regex": regex, "sort": sort, "multi": multi, "includeAll": include_all,
         "hide": hide, "options": []}
    if include_all:
        v["allValue"] = all_value
    if current_all and include_all:
        v["current"] = {"selected": True, "text": ["All"], "value": ["$__all"]}
    return v


def dashboard(uid, title, panels, variables, tags, desc, refresh="30s", time_from="now-3h", links=None):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["live-commerce"] + tags,
        "editable": True, "graphTooltip": 1, "timezone": "browser", "refresh": refresh,
        "schemaVersion": 41, "version": 1, "fiscalYearStartMonth": 0, "liveNow": False,
        "time": {"from": time_from, "to": "now"},
        "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m", "15m"]},
        "templating": {"list": variables}, "annotations": {"list": [{
            "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
            "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts",
            "type": "dashboard"}]},
        "links": links or [{"title": "Live Commerce", "type": "dashboards", "tags": ["live-commerce"],
                            "asDropdown": True, "includeVars": False, "keepTime": True}],
        "panels": panels,
    }


# =====================================================================================
# 01 — Tổng quan dịch vụ (NOC / điều hành)
# =====================================================================================
def overview():
    g = Grid()
    ns = 'namespace=~"$namespace"'
    lvars = [
        v_ds("ds_prom", "prometheus", "Prometheus"),
        v_ds("ds_loki", "loki", "Loki"),
        v_query("namespace", "Namespace", "label_values(kube_deployment_spec_replicas, namespace)",
                regex=f"/^({APP_NS}|live-commerce|moderation)$/", all_value=APP_NS + "|live-commerce|moderation"),
        v_query("service", "Service (route Kong)", "label_values(kube_deployment_spec_replicas{namespace=~\"$namespace\"}, deployment)"),
    ]
    g.add(text(
        "**Live Commerce – NOC.** Traffic/lỗi/đơn hàng lấy từ **log Kong qua Loki** (app chưa có `/metrics`, "
        "Prometheus CMC không scrape Kong). Hạ tầng lấy từ Prometheus CMC (`cmc-monitoring`, retention **1 ngày**). "
        "Latency p95 chỉ có sau khi bật log format `lc_combined` cho Kong."), 24, 2)

    g.row("Sức khỏe tổng quan")
    g.add(stat("Deployment sẵn sàng",
               [tgt(f'sum(kube_deployment_status_replicas_available{{{ns}}}) / sum(kube_deployment_spec_replicas{{{ns}}})', instant=True)],
               unit="percentunit", decimals=0, th=thresholds((None, "red"), (0.8, "orange"), (1, "green")),
               desc="Tổng replica available / desired của mọi Deployment trong namespace đã chọn."), 4, 4)
    g.add(stat("Request/s (Kong)", [tgt(f'sum(rate({KONG} [5m]))', ds=LOKI, instant=True)],
               unit="reqps", decimals=2, color_mode="value", graph="area", ds=LOKI,
               desc="Tổng request qua Kong (không tính health-check của ingress-controller)."), 4, 4)
    g.add(stat("Tỷ lệ lỗi 5xx",
               [tgt(f'(sum(rate({KONG} | status=~"5.." [5m])) or vector(0)) / sum(rate({KONG} [5m]))', ds=LOKI, instant=True)],
               unit="percentunit", decimals=2, ds=LOKI,
               th=thresholds((None, "green"), (0.01, "orange"), (0.05, "red")),
               desc="5xx / tổng request trong 5 phút. SLO 99,9 % ⇒ ngưỡng đỏ 5 %."), 4, 4)
    g.add(stat("Đơn đặt thành công (khoảng chọn)",
               [tgt(f'sum(count_over_time({KONG} | method="POST" | path=~"/api/orders/?" | status=~"2.." [$__range]))', ds=LOKI, instant=True)],
               decimals=0, color_mode="value", ds=LOKI, th=thresholds((None, "blue")),
               desc="POST /api/orders trả 2xx trong khoảng thời gian đang chọn."), 4, 4)
    g.add(stat("Phiên live mở (khoảng chọn)",
               [tgt('sum(count_over_time({namespace=~"$namespace", app="live-manager"} |= "mở phiên" [$__range]))', ds=LOKI, instant=True)],
               decimals=0, color_mode="value", ds=LOKI, th=thresholds((None, "purple")),
               desc='Đếm log live-manager "Seller … mở phiên …".'), 4, 4)
    g.add(stat("Pod restart (1h)",
               [tgt(f'sum(increase(kube_pod_container_status_restarts_total{{{ns}}}[1h])) or vector(0)', instant=True)],
               decimals=0, th=thresholds((None, "green"), (1, "orange"), (5, "red"))), 4, 4)

    g.row("Traffic & lỗi qua Kong (theo service)")
    g.add(ts("Request/s theo service", [tgt(f'sum by (svc) (rate({KONG} | svc=~"$service" [$__auto]))', "{{svc}}", ds=LOKI)],
             unit="reqps", stack=True, ds=LOKI), 12, 8)
    g.add(ts("Request/s theo nhóm status", [tgt(
        f'sum by (sc) (rate({KONG} | svc=~"$service" | label_format sc=`{{{{ substr 0 1 .status }}}}xx` [$__auto]))', "{{sc}}", ds=LOKI)],
             unit="reqps", stack=True, ds=LOKI, overrides=[
                 unit_override("2xx", color="green"), unit_override("3xx", color="blue"),
                 unit_override("4xx", color="orange"), unit_override("5xx", color="red")]), 12, 8)
    g.add(ts("Tỷ lệ 5xx theo service", [tgt(
        f'sum by (svc) (rate({KONG} | svc=~"$service" | status=~"5.." [$__auto])) / sum by (svc) (rate({KONG} | svc=~"$service" [$__auto]))',
        "{{svc}}", ds=LOKI)], unit="percentunit", ds=LOKI, th_style="line+area",
             th=thresholds((None, "transparent"), (0.05, "red")), min_=0), 12, 8)
    g.add(ts("Latency p95 / p99 theo service (cần log format lc_combined)", [
        tgt(f'quantile_over_time(0.95, {KONG_RT} | svc=~"$service" [$__auto]) by (svc)', "p95 {{svc}}", ds=LOKI),
        tgt(f'quantile_over_time(0.99, {KONG_RT} | svc=~"$service" [$__auto]) by (svc)', "p99 {{svc}}", ds=LOKI, ref="B")],
             unit="s", ds=LOKI, th_style="line", th=thresholds((None, "transparent"), (0.5, "orange"), (1, "red")),
             desc="Thời gian Kong xử lý request ($request_time). Trống nếu Kong vẫn dùng log format mặc định."), 12, 8)
    g.add(table("Top endpoint lỗi (4xx/5xx) trong khoảng chọn", [tgt(
        f'topk(15, sum by (method, path, status) (count_over_time({KONG} | svc=~"$service" | status=~"[45].." [$__range])))',
        ds=LOKI, instant=True, fmt="table")], ds=LOKI,
        transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                        "renameByName": {"Value": "Số lần"}}}],
        sort=[{"displayName": "Số lần", "desc": True}]), 12, 8)
    g.add(table("Top endpoint theo lưu lượng", [tgt(
        f'topk(15, sum by (method, path) (count_over_time({KONG} | svc=~"$service" [$__range])))',
        ds=LOKI, instant=True, fmt="table")], ds=LOKI,
        transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                        "renameByName": {"Value": "Request"}}}],
        sort=[{"displayName": "Request", "desc": True}]), 12, 8)

    g.row("Thương mại (đơn hàng, tồn kho)")
    g.add(ts("Đơn hàng / phút theo status (POST /api/orders)", [tgt(
        f'sum by (status) (count_over_time({KONG} | method="POST" | path=~"/api/orders/?" [1m]))', "{{status}}", ds=LOKI)],
             unit="short", stack=True, ds=LOKI, draw="bars", fill=80,
             desc="2xx = đặt thành công; 409/4xx thường là hết hàng hoặc dữ liệu sai; 5xx = lỗi hệ thống."), 12, 8)
    g.add(ts("Lỗi nghiệp vụ đơn hàng (log order-service)", [
        tgt('sum(count_over_time({namespace=~"$namespace", app="order-service"} |= "Ghi đơn thất bại" [1m]))', "Ghi đơn thất bại", ds=LOKI),
        tgt('sum(count_over_time({namespace=~"$namespace", app="order-service"} |= "Hoàn tồn kho thất bại" [1m]))', "Hoàn tồn kho thất bại (lệch tồn kho!)", ds=LOKI, ref="B")],
             ds=LOKI, draw="bars", fill=80,
             overrides=[unit_override("Hoàn tồn kho thất bại (lệch tồn kho!)", color="dark-red")]), 12, 8)

    g.row("Livestream (IVS, chat, kiểm duyệt)")
    g.add(ts("Kết nối WebSocket mới / phút (chat)", [tgt(
        f'sum(count_over_time({KONG} | svc="chat-service" | status="101" [1m]))', "WS upgrade 101", ds=LOKI),
        tgt(f'sum(count_over_time({KONG} | svc="chat-service" | status!="101" [1m]))', "chat non-101", ds=LOKI, ref="B")],
             ds=LOKI, desc="Số handshake WebSocket thành công. Tăng đột biến = viewer đổ vào phiên live."), 8, 8)
    g.add(ts("Phiên live mở & lỗi AWS IVS", [
        tgt('sum(count_over_time({namespace=~"$namespace", app="live-manager"} |= "mở phiên" [5m]))', "Phiên mở (5m)", ds=LOKI),
        tgt('sum(count_over_time({namespace=~"$namespace", app="live-manager"} |~ "(PutMetadata|CreateStreamKey|ListStreams) lỗi|Không xóa được kênh" [5m]))',
            "Lỗi IVS (5m)", ds=LOKI, ref="B")], ds=LOKI, draw="bars", fill=80,
             overrides=[unit_override("Lỗi IVS (5m)", color="red")]), 8, 8)
    g.add(ts("Sự kiện kiểm duyệt (moderation)", [tgt(
        'sum by (kind) (count_over_time({namespace=~"$namespace", app="moderation-service"} |= "Moderation event" '
        '| regexp `Moderation event \\S+: (?P<kind>[A-Z_]+/[A-Z_]+)` [5m]))', "{{kind}}", ds=LOKI)],
             ds=LOKI, draw="bars", fill=80, stack=True,
             desc="IMAGE_VIOLATION/STREAM_STOPPED = phiên bị dừng do vi phạm."), 8, 8)
    g.add(ts("chat-service & order-service: replica so với HPA", [
        tgt(f'kube_horizontalpodautoscaler_status_current_replicas{{{ns}}}', "current {{horizontalpodautoscaler}}"),
        tgt(f'kube_horizontalpodautoscaler_spec_max_replicas{{{ns}}}', "max {{horizontalpodautoscaler}}", ref="B")],
             desc="Chạm max trong giờ live = cần nâng maxReplicas hoặc node.",
             overrides=[{"matcher": {"id": "byRegexp", "options": "max .*"},
                         "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                        {"id": "custom.fillOpacity", "value": 0}]}]), 12, 8)
    g.add(ts("Log lỗi / phút theo service", [tgt(
        'sum by (app) (count_over_time({namespace=~"$namespace"} |~ "(?i)\\b(error|exception|fatal)\\b" [1m]))', "{{app}}", ds=LOKI)],
             ds=LOKI, stack=True), 12, 8)

    g.row("Log lỗi gần nhất", collapsed=False)
    g.add(logs("Log ERROR/WARN của ứng dụng",
               '{namespace=~"$namespace"} |~ "(?i)\\b(error|warn|exception|fatal)\\b"'), 24, 10)
    return dashboard("lc-overview", "Live Commerce / 01 · Tổng quan dịch vụ", g.panels, lvars,
                     ["noc", "business"], "Golden signals, đơn hàng, livestream – màn hình NOC.")


# =====================================================================================
# 02 — Workloads & Autoscaling
# =====================================================================================
def workloads():
    g = Grid()
    sel = 'namespace=~"$namespace", pod=~"($deployment)-[a-z0-9]{8,10}-[a-z0-9]{5}"'
    csel = sel + ', container!=""'
    vars_ = [
        v_ds("ds_prom", "prometheus", "Prometheus"),
        v_ds("ds_loki", "loki", "Loki"),
        v_query("namespace", "Namespace", "label_values(kube_deployment_spec_replicas, namespace)",
                multi=False, include_all=False, current_all=False, regex=f"/^({APP_NS}|live-commerce|moderation)$/"),
        v_query("deployment", "Deployment", 'label_values(kube_deployment_spec_replicas{namespace=~"$namespace"}, deployment)'),
        v_query("pod", "Pod", f'label_values(kube_pod_info{{{sel}}}, pod)'),
    ]
    psel = sel + ', pod=~"$pod"'
    pcsel = psel + ', container!=""'

    g.row("Trạng thái Deployment")
    g.add(table("Deployment: desired / available / updated", [
        tgt('kube_deployment_spec_replicas{namespace=~"$namespace", deployment=~"$deployment"}', instant=True, fmt="table", ref="A"),
        tgt('kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"$deployment"}', instant=True, fmt="table", ref="B"),
        tgt('kube_deployment_status_replicas_updated{namespace=~"$namespace", deployment=~"$deployment"}', instant=True, fmt="table", ref="C"),
        tgt('kube_deployment_status_replicas_unavailable{namespace=~"$namespace", deployment=~"$deployment"}', instant=True, fmt="table", ref="D"),
        tgt(f'sum by (deployment) ({POD_DEPLOY.format(m="increase(kube_pod_container_status_restarts_total{" + sel + "}[1h])")})', instant=True, fmt="table", ref="E"),
    ], transformations=[
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "namespace": True},
            "renameByName": {"Value #A": "Desired", "Value #B": "Available", "Value #C": "Updated",
                             "Value #D": "Unavailable", "Value #E": "Restart 1h", "deployment": "Deployment"}}}],
        overrides=[cell_color("Unavailable", "short", thresholds((None, "green"), (1, "red"))),
                   cell_color("Restart 1h", "short", thresholds((None, "green"), (1, "orange"), (5, "red")))]), 14, 8)
    g.add(table("HPA", [
        tgt('kube_horizontalpodautoscaler_status_current_replicas{namespace=~"$namespace"}', instant=True, fmt="table", ref="A"),
        tgt('kube_horizontalpodautoscaler_status_desired_replicas{namespace=~"$namespace"}', instant=True, fmt="table", ref="B"),
        tgt('kube_horizontalpodautoscaler_spec_min_replicas{namespace=~"$namespace"}', instant=True, fmt="table", ref="C"),
        tgt('kube_horizontalpodautoscaler_spec_max_replicas{namespace=~"$namespace"}', instant=True, fmt="table", ref="D"),
        tgt('kube_horizontalpodautoscaler_status_current_replicas{namespace=~"$namespace"} / kube_horizontalpodautoscaler_spec_max_replicas{namespace=~"$namespace"}',
            instant=True, fmt="table", ref="E"),
    ], transformations=[
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "namespace": True},
            "renameByName": {"horizontalpodautoscaler": "HPA", "Value #A": "Current", "Value #B": "Desired",
                             "Value #C": "Min", "Value #D": "Max", "Value #E": "% của Max"}}}],
        overrides=[cell_color("% của Max", "percentunit", thresholds((None, "green"), (0.8, "orange"), (1, "red")),
                              "gauge")]), 10, 8)
    g.add({"type": "state-timeline", "title": "Replica unavailable theo thời gian", "datasource": PROM,
           "targets": [tgt('kube_deployment_status_replicas_unavailable{namespace=~"$namespace", deployment=~"$deployment"}', "{{deployment}}")],
           "fieldConfig": {"defaults": {"thresholds": thresholds((None, "green"), (1, "red")),
                                        "color": {"mode": "thresholds"}, "custom": {"fillOpacity": 80}},
                           "overrides": []},
           "options": {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8,
                       "legend": {"showLegend": False}}}, 24, 6)

    g.row("CPU")
    g.add(ts("CPU sử dụng theo pod (core)", [
        tgt(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{pcsel}}}[$__rate_interval]))', "{{pod}}")],
             unit="none", desc="cadvisor, đã lọc container!=\"\" để bỏ series cấp pod (tránh đếm đôi)."), 12, 8)
    g.add(ts("CPU % so với request (HPA tính theo đây)", [
        tgt("sum by (deployment) (" + POD_DEPLOY.format(m="rate(container_cpu_usage_seconds_total{" + pcsel + "}[$__rate_interval])") + ")"
            " / sum by (deployment) (" + POD_DEPLOY.format(m="kube_pod_container_resource_requests{" + psel + ', resource="cpu"}') + ")", "{{deployment}}")],
             unit="percentunit", th_style="line", th=thresholds((None, "transparent"), (0.7, "orange")),
             desc="Đường cam 70 % = target HPA."), 12, 8)
    g.add(ts("CPU throttling %", [
        tgt(f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{pcsel}}}[$__rate_interval]))'
            f' / sum by (pod) (rate(container_cpu_cfs_periods_total{{{pcsel}}}[$__rate_interval]))', "{{pod}}")],
             unit="percentunit", th_style="line", th=thresholds((None, "transparent"), (0.25, "red")),
             desc="Node.js bị throttle sẽ tăng latency chat/đặt hàng dù CPU trung bình thấp."), 12, 8)
    g.add(ts("CPU so với limit (core)", [
        tgt(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{pcsel}}}[$__rate_interval]))', "used {{pod}}"),
        tgt(f'sum by (pod) (kube_pod_container_resource_limits{{{psel}, resource="cpu"}})', "limit {{pod}}", ref="B")],
             overrides=[{"matcher": {"id": "byRegexp", "options": "limit .*"},
                         "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                        {"id": "custom.fillOpacity", "value": 0}]}]), 12, 8)

    g.row("Memory")
    g.add(ts("Memory working set theo pod", [
        tgt(f'sum by (pod) (container_memory_working_set_bytes{{{pcsel}}})', "{{pod}}")], unit="bytes"), 12, 8)
    g.add(ts("Memory % so với limit (OOMKill ở 100 %)", [
        tgt(f'sum by (pod) (container_memory_working_set_bytes{{{pcsel}}}) / sum by (pod) (kube_pod_container_resource_limits{{{psel}, resource="memory"}})', "{{pod}}")],
             unit="percentunit", max_=1, th_style="line+area", th=thresholds((None, "transparent"), (0.9, "red"))), 12, 8)

    g.row("Ổn định & mạng")
    g.add(ts("Restart (tăng trong 5m)", [
        tgt(f'sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{psel}}}[5m])) > 0', "{{pod}}/{{container}}")],
             draw="bars", fill=80), 8, 8)
    g.add(ts("OOM events", [
        tgt(f'sum by (pod) (increase(container_oom_events_total{{{pcsel}}}[5m])) > 0', "{{pod}}")], draw="bars", fill=80), 8, 8)
    g.add(table("Container đang Waiting / lần chết gần nhất", [
        tgt('kube_pod_container_status_waiting_reason{namespace=~"$namespace"} > 0', instant=True, fmt="table", ref="A"),
        tgt('kube_pod_container_status_last_terminated_reason{namespace=~"$namespace"} > 0', instant=True, fmt="table", ref="B")],
        transformations=[{"id": "merge", "options": {}}, {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "uid": True, "Value #A": True, "Value #B": True}}}]),
        8, 8)
    g.add(ts("Network theo pod (eth0)", [
        tgt(f'sum by (pod) (rate(container_network_receive_bytes_total{{{psel}, interface="eth0"}}[$__rate_interval]))', "rx {{pod}}"),
        tgt(f'-sum by (pod) (rate(container_network_transmit_bytes_total{{{psel}, interface="eth0"}}[$__rate_interval]))', "tx {{pod}}", ref="B")],
             unit="Bps", desc="cadvisor CMC chỉ ghi network ở pause container; lọc eth0 để bỏ tunl0 (Calico IPIP). tx vẽ âm."), 12, 8)
    g.add(ts("Network lỗi/drop theo pod", [
        tgt(f'sum by (pod) (rate(container_network_receive_packets_dropped_total{{{psel}, interface="eth0"}}[$__rate_interval]) + rate(container_network_transmit_packets_dropped_total{{{psel}, interface="eth0"}}[$__rate_interval]))', "drop {{pod}}"),
        tgt(f'sum by (pod) (rate(container_network_receive_errors_total{{{psel}, interface="eth0"}}[$__rate_interval]) + rate(container_network_transmit_errors_total{{{psel}, interface="eth0"}}[$__rate_interval]))', "err {{pod}}", ref="B")],
             unit="pps"), 12, 8)

    g.row("Log")
    # Lọc theo deployment (regex tên pod) + $pod: $pod=All (".+") vẫn chỉ ra log của deployment đã chọn,
    # kể cả pod cũ đã bị rollout thay (biến $pod lấy từ Prometheus nên chỉ liệt kê pod đang sống).
    g.add(logs("Log của pod đã chọn", f'{{{psel}}}'), 24, 12)
    return dashboard("lc-workloads", "Live Commerce / 02 · Workloads & Autoscaling", g.panels, vars_,
                     ["workloads", "hpa"], "Deployment, HPA, CPU/memory/throttling/OOM, network theo pod.", time_from="now-1h")


# =====================================================================================
# 03 — Cluster & Nodes (Prometheus CMC)
# =====================================================================================
def cluster():
    g = Grid()
    ng_q = ('query_result(count by (nodegroup) (' + NODEGROUP.format(m="kube_node_info", l="node") + '))')
    vars_ = [
        v_ds("ds_prom", "prometheus", "Prometheus"),
        v_ds("ds_loki", "loki", "Loki"),
        v_query("nodegroup", "Node group", ng_q, regex='/nodegroup="([^"]+)"/'),
        v_query("node", "Node", 'label_values(kube_node_info{node=~".*-(nodegroup-)?(${nodegroup:pipe})-.*"}, node)'),
    ]
    kn = 'node=~"$node"'        # kube-state-metrics
    nn = 'node_name=~"$node"'   # node-exporter / cadvisor

    g.row("Tổng quan cluster")
    g.add(stat("Node Ready", [tgt(f'sum(kube_node_status_condition{{condition="Ready", status="true", {kn}}})', instant=True),
                              ], decimals=0, color_mode="value", th=thresholds((None, "green"))), 3, 4)
    g.add(stat("Node NotReady", [tgt(f'sum(kube_node_status_condition{{condition="Ready", status!="true", {kn}}}) or vector(0)', instant=True)],
               decimals=0, th=thresholds((None, "green"), (1, "red"))), 3, 4)
    g.add(stat("Node cordon", [tgt(f'sum(kube_node_spec_unschedulable{{{kn}}}) or vector(0)', instant=True)],
               decimals=0, th=thresholds((None, "green"), (1, "orange"))), 3, 4)
    g.add(stat("CPU requested / allocatable", [tgt(
        f'sum(kube_pod_container_resource_requests{{resource="cpu", {kn}}}) / sum(kube_node_status_allocatable{{resource="cpu", {kn}}})', instant=True)],
               unit="percentunit", decimals=1, th=thresholds((None, "green"), (0.75, "orange"), (0.9, "red")),
               desc="Cao = HPA không còn chỗ scale khi có phiên live lớn."), 3, 4)
    g.add(stat("Mem requested / allocatable", [tgt(
        f'sum(kube_pod_container_resource_requests{{resource="memory", {kn}}}) / sum(kube_node_status_allocatable{{resource="memory", {kn}}})', instant=True)],
               unit="percentunit", decimals=1, th=thresholds((None, "green"), (0.75, "orange"), (0.9, "red"))), 3, 4)
    g.add(stat("CPU dùng thực tế", [tgt(
        f'1 - avg(rate(node_cpu_seconds_total{{mode="idle", {nn}}}[5m]))', instant=True)],
               unit="percentunit", decimals=1, th=GREEN_RED_FRAC), 3, 4)
    g.add(stat("Mem dùng thực tế", [tgt(
        f'1 - sum(node_memory_MemAvailable_bytes{{{nn}}}) / sum(node_memory_MemTotal_bytes{{{nn}}})', instant=True)],
               unit="percentunit", decimals=1, th=GREEN_RED_FRAC), 3, 4)
    g.add(stat("Pod / capacity", [tgt(
        f'sum(kube_pod_info{{{kn}}}) / sum(kube_node_status_allocatable{{resource="pods", {kn}}})', instant=True)],
               unit="percentunit", decimals=1, th=GREEN_RED_FRAC), 3, 4)

    g.add(table("Node (nhóm, tài nguyên, đĩa, pod)", [
        tgt(NODEGROUP.format(m=f'kube_node_info{{{kn}}}', l="node"), instant=True, fmt="table", ref="A"),
        tgt(f'label_replace(1 - avg by (node_name) (rate(node_cpu_seconds_total{{mode="idle", {nn}}}[5m])), "node", "$1", "node_name", "(.*)")', instant=True, fmt="table", ref="B"),
        tgt(f'label_replace(1 - node_memory_MemAvailable_bytes{{{nn}}} / node_memory_MemTotal_bytes{{{nn}}}, "node", "$1", "node_name", "(.*)")', instant=True, fmt="table", ref="C"),
        tgt(f'label_replace(1 - node_filesystem_avail_bytes{{mountpoint="/", {nn}}} / node_filesystem_size_bytes{{mountpoint="/", {nn}}}, "node", "$1", "node_name", "(.*)")', instant=True, fmt="table", ref="D"),
        tgt(f'sum by (node) (kube_pod_container_resource_requests{{resource="cpu", {kn}}}) / sum by (node) (kube_node_status_allocatable{{resource="cpu", {kn}}})', instant=True, fmt="table", ref="E"),
        tgt(f'sum by (node) (kube_pod_container_resource_requests{{resource="memory", {kn}}}) / sum by (node) (kube_node_status_allocatable{{resource="memory", {kn}}})', instant=True, fmt="table", ref="F"),
        tgt(f'count by (node) (kube_pod_info{{{kn}}})', instant=True, fmt="table", ref="G"),
        tgt(f'label_replace(node_load5{{{nn}}} / on (node_name) count by (node_name) (node_cpu_seconds_total{{mode="idle", {nn}}}), "node", "$1", "node_name", "(.*)")', instant=True, fmt="table", ref="H"),
    ], transformations=[
        {"id": "joinByField", "options": {"byField": "node", "mode": "outer"}},
        {"id": "organize", "options": {
            "excludeByName": {**{f"{c}{s}": True for c in ("Time", "__name__", "instance", "job", "node_name", "device", "fstype", "mountpoint")
                                 for s in ("", " 1", " 2", " 3", " 4", " 5", " 6", " 7", " 8")},
                              "Value #A": True, "container_runtime_version": True, "kernel_version": True,
                              "os_image": True, "pod_cidr": True, "provider_id": True, "system_uuid": True,
                              "kubelet_version": True},
            "indexByName": {"node": 0, "nodegroup": 1, "internal_ip": 2},
            "renameByName": {"Value #B": "CPU dùng", "Value #C": "Mem dùng", "Value #D": "Đĩa /",
                             "Value #E": "CPU req", "Value #F": "Mem req", "Value #G": "Pod",
                             "Value #H": "Load5/core", "internal_ip": "IP", "nodegroup": "Node group", "node": "Node"}}}],
        overrides=[cell_color(n, "percentunit", GREEN_RED_FRAC, "color-background") for n in
                   ("CPU dùng", "Mem dùng", "Đĩa /", "CPU req", "Mem req")] +
                  [cell_color("Load5/core", "short", thresholds((None, "green"), (1, "orange"), (2, "red")))],
        sort=[{"displayName": "CPU dùng", "desc": True}]), 24, 10)

    g.row("Node: CPU / Memory / Đĩa / Mạng")
    g.add(ts("CPU % theo node", [tgt(f'1 - avg by (node_name) (rate(node_cpu_seconds_total{{mode="idle", {nn}}}[$__rate_interval]))', "{{node_name}}")],
             unit="percentunit", max_=1, th_style="line", th=thresholds((None, "transparent"), (0.85, "red"))), 12, 8)
    g.add(ts("Memory % theo node", [tgt(f'1 - node_memory_MemAvailable_bytes{{{nn}}} / node_memory_MemTotal_bytes{{{nn}}}', "{{node_name}}")],
             unit="percentunit", max_=1, th_style="line", th=thresholds((None, "transparent"), (0.9, "red"))), 12, 8)
    g.add(ts("CPU iowait / steal theo node", [
        tgt(f'avg by (node_name) (rate(node_cpu_seconds_total{{mode="iowait", {nn}}}[$__rate_interval]))', "iowait {{node_name}}"),
        tgt(f'avg by (node_name) (rate(node_cpu_seconds_total{{mode="steal", {nn}}}[$__rate_interval]))', "steal {{node_name}}", ref="B")],
             unit="percentunit", desc="steal cao = VM bị host OpenStack của CMC tranh CPU (noisy neighbour)."), 12, 8)
    g.add(ts("Đĩa / dùng % (+ dự báo đầy)", [tgt(
        f'1 - node_filesystem_avail_bytes{{mountpoint="/", {nn}}} / node_filesystem_size_bytes{{mountpoint="/", {nn}}}', "{{node_name}}")],
             unit="percentunit", max_=1, th_style="line", th=thresholds((None, "transparent"), (0.85, "red"))), 12, 8)
    g.add(ts("Mạng node (eth0)", [
        tgt(f'rate(node_network_receive_bytes_total{{device="eth0", {nn}}}[$__rate_interval])', "rx {{node_name}}"),
        tgt(f'-rate(node_network_transmit_bytes_total{{device="eth0", {nn}}}[$__rate_interval])', "tx {{node_name}}", ref="B")],
             unit="Bps"), 12, 8)
    g.add(ts("Disk IO time (vda)", [tgt(
        f'rate(node_disk_io_time_seconds_total{{device=~"vd[a-z]+", {nn}}}[$__rate_interval])', "{{node_name}} {{device}}")],
             unit="percentunit"), 12, 8)

    g.row("Phân bổ tài nguyên theo namespace")
    g.add(bargauge("CPU request theo namespace (core)", [tgt(
        f'sort_desc(sum by (namespace) (kube_pod_container_resource_requests{{resource="cpu", {kn}}}))', "{{namespace}}", instant=True)],
                   unit="none", max_=None, th=thresholds((None, "blue"))), 8, 9)
    g.add(ts("CPU dùng theo namespace (core)", [tgt(
        f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{container!="", {nn}}}[$__rate_interval]))', "{{namespace}}")],
             stack=True), 8, 9)
    g.add(ts("Memory dùng theo namespace", [tgt(
        f'sum by (namespace) (container_memory_working_set_bytes{{container!="", {nn}}})', "{{namespace}}")],
             unit="bytes", stack=True), 8, 9)

    g.row("Control plane & hệ thống giám sát")
    g.add(stat("API server up", [tgt('sum(up{job="kubernetes-apiservers", instance=~".*:6443"}) / count(up{job="kubernetes-apiservers", instance=~".*:6443"})', instant=True)],
               unit="percentunit", decimals=0, th=thresholds((None, "red"), (0.67, "orange"), (1, "green"))), 4, 5)
    g.add(ts("API server request/s theo code", [tgt(
        'sum by (code) (rate(apiserver_request_total[$__rate_interval]))', "{{code}}")], unit="reqps", stack=True), 10, 5)
    g.add(table("Scrape target theo job", [
        tgt('count by (job) (up{instance!~"10\\\\..*:80"})', instant=True, fmt="table", ref="A"),
        tgt('sum by (job) (up{instance!~"10\\\\..*:80"})', instant=True, fmt="table", ref="B")],
        transformations=[{"id": "merge", "options": {}}, {"id": "organize", "options": {
            "excludeByName": {"Time": True}, "renameByName": {"Value #A": "Tổng", "Value #B": "Up"}}}]), 10, 5)
    g.add(ts("DaemonSet chưa đủ pod", [tgt(
        'kube_daemonset_status_desired_number_scheduled - kube_daemonset_status_number_ready > 0', "{{namespace}}/{{daemonset}}")],
             draw="bars", fill=80), 24, 6)
    return dashboard("lc-cluster", "Live Commerce / 03 · Cluster & Nodes (CMC)", g.panels, vars_,
                     ["cluster", "nodes", "cmc"], "Node, nodegroup, capacity – dùng đúng nhãn của Prometheus CMC.",
                     time_from="now-6h")


GREEN_RED_FRAC = thresholds((None, "green"), (0.8, "orange"), (0.9, "red"))


# =====================================================================================
# Alert rules (Grafana-managed, provisioning file format)
# =====================================================================================
def q_prom(expr, ref="A", rng=600):
    return {"refId": ref, "relativeTimeRange": {"from": rng, "to": 0}, "datasourceUid": PROM_UID,
            "model": {"refId": ref, "expr": expr, "instant": True, "range": False, "intervalMs": 1000, "maxDataPoints": 43200}}


def q_loki(expr, ref="A", rng=600):
    return {"refId": ref, "relativeTimeRange": {"from": rng, "to": 0}, "datasourceUid": LOKI_UID,
            "model": {"refId": ref, "expr": expr, "queryType": "instant", "intervalMs": 1000, "maxDataPoints": 43200}}


def thresh(op, value, ref="C", src="A"):
    return {"refId": ref, "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0},
            "model": {"refId": ref, "type": "threshold", "expression": src,
                      "conditions": [{"evaluator": {"type": op, "params": [value]}}]}}


def rule(uid, title, query, op, value, for_, severity, summary, description, dash=None, panel=None,
         no_data="OK", team="platform", runbook=None, keep_firing=None):
    ann = {"summary": summary, "description": description}
    if dash:
        ann["__dashboardUid__"] = dash
        if panel:
            ann["__panelId__"] = str(panel)
    ann["runbook_url"] = runbook or f"{RB}#{uid}"
    r = {"uid": uid, "title": title, "condition": "C", "data": [query, thresh(op, value)],
         "noDataState": no_data, "execErrState": "Error", "for": for_, "isPaused": False,
         "annotations": ann, "labels": {"severity": severity, "team": team, "product": "live-commerce"}}
    if keep_firing:
        r["keep_firing_for"] = keep_firing
    return r


# Trang runbook tĩnh (platform/monitoring/runbook/index.html, host trên Vercel); anchor = uid của rule.
RB = "https://runbooklive-e-commerce.vercel.app/"


def alert_rules():
    ns = 'namespace=~"live-commerce.*|moderation.*"'
    groups = []

    # ---------- Ứng dụng: tính sẵn sàng ----------
    groups.append({"orgId": 1, "name": "lc-app-availability", "folder": "Live Commerce", "interval": "1m", "rules": [
        rule("lc-deploy-unavailable", "Deployment thiếu replica",
             q_prom(f'sum by (namespace, deployment) (kube_deployment_spec_replicas{{{ns}}}) - sum by (namespace, deployment) (kube_deployment_status_replicas_available{{{ns}}})'),
             "gt", 0, "5m", "critical",
             "{{ .Labels.namespace }}/{{ .Labels.deployment }} thiếu {{ printf \"%.0f\" .Values.A.Value }} replica",
             "Deployment không đạt số replica mong muốn trong 5 phút. Kiểm tra `kubectl -n {{ .Labels.namespace }} describe deploy {{ .Labels.deployment }}` (ImagePullBackOff từ Harbor, thiếu tài nguyên node, probe lỗi).",
             "lc-workloads"),
        rule("lc-deploy-down", "Service DOWN hoàn toàn (0 replica available)",
             q_prom(f'count by (namespace, deployment) ((kube_deployment_status_replicas_available{{{ns}}} == 0) and on (namespace, deployment) (kube_deployment_spec_replicas{{{ns}}} > 0))'),
             "gt", 0, "1m", "critical",
             "🔥 {{ .Labels.namespace }}/{{ .Labels.deployment }} không còn pod nào chạy",
             "Toàn bộ pod của service đã mất – người dùng đang bị ảnh hưởng trực tiếp.", "lc-workloads"),
        rule("lc-container-waiting", "Container kẹt Waiting (ImagePullBackOff / CrashLoopBackOff …)",
             q_prom(f'sum by (namespace, pod, reason) (kube_pod_container_status_waiting_reason{{{ns}}})'),
             "gt", 0, "10m", "warning",
             "{{ .Labels.namespace }}/{{ .Labels.pod }} Waiting: {{ .Labels.reason }}",
             "ImagePullBackOff thường do robot Harbor hết hạn hoặc CA Harbor chưa có trên node mới.", "lc-workloads"),
        rule("lc-hpa-maxed", "HPA chạm maxReplicas",
             q_prom(f'kube_horizontalpodautoscaler_status_current_replicas{{{ns}}} / kube_horizontalpodautoscaler_spec_max_replicas{{{ns}}}'),
             "gte", 1, "3m", "warning",
             "HPA {{ .Labels.namespace }}/{{ .Labels.horizontalpodautoscaler }} đã ở max replica 3 phút",
             "Không thể scale thêm khi lượng viewer/đơn tiếp tục tăng. Tăng maxReplicas hoặc thêm node trước phiên live lớn.", "lc-workloads"),
        rule("lc-cpu-throttle", "CPU throttling cao",
             q_prom(f'sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total{{{ns}, container!=""}}[5m])) / sum by (namespace, pod) (rate(container_cpu_cfs_periods_total{{{ns}, container!=""}}[5m]))'),
             "gt", 0.25, "15m", "warning",
             "{{ .Labels.namespace }}/{{ .Labels.pod }} bị throttle {{ humanizePercentage .Values.A.Value }}",
             "Pod bị giới hạn CPU, latency chat/đặt hàng sẽ tăng. Xem lại CPU limit.", "lc-workloads"),
        rule("lc-mem-near-limit", "Memory gần chạm limit",
             q_prom(f'sum by (namespace, pod) (container_memory_working_set_bytes{{{ns}, container!=""}}) / sum by (namespace, pod) (kube_pod_container_resource_limits{{{ns}, resource="memory"}})'),
             "gt", 0.9, "10m", "warning",
             "{{ .Labels.namespace }}/{{ .Labels.pod }} dùng {{ humanizePercentage .Values.A.Value }} memory limit",
             "Sắp bị OOMKill.", "lc-workloads"),
    ]})

    # ---------- Traffic & nghiệp vụ (Loki) ----------
    groups.append({"orgId": 1, "name": "lc-traffic-business", "folder": "Live Commerce", "interval": "1m", "rules": [
        rule("lc-kong-5xx-ratio", "Tỷ lệ 5xx qua Kong cao",
             q_loki(f'(sum by (svc) (rate({KONG_RULE} | status=~"5.." [5m])) / sum by (svc) (rate({KONG_RULE} [5m]))) and on (svc) (sum by (svc) (count_over_time({KONG_RULE} [5m])) > 20)'),
             "gt", 0.05, "3m", "critical",
             "{{ .Labels.svc }}: {{ humanizePercentage .Values.A.Value }} request lỗi 5xx",
             "Hơn 5 % request tới {{ .Labels.svc }} lỗi 5xx trong 5 phút (chỉ xét khi > 20 request). Vi phạm SLO 99,9 %.",
             "lc-overview"),
        rule("lc-kong-upstream-down", "Kong không gọi được upstream (502/503/504)",
             q_loki(f'sum by (svc, status) (count_over_time({KONG_RULE} | status=~"50[234]" [5m]))'),
             "gt", 10, "0s", "critical",
             "{{ .Labels.svc }} trả {{ .Labels.status }} – {{ printf \"%.0f\" .Values.A.Value }} lần/5m",
             "Kong không kết nối được service (pod chết, readiness fail hoặc timeout).", "lc-overview"),
        rule("lc-order-write-fail", "Ghi đơn hàng thất bại",
             q_loki('sum(count_over_time({namespace=~"live-commerce.*", app="order-service"} |= "Ghi đơn thất bại" [5m]))'),
             "gt", 0, "0s", "critical",
             "💸 {{ printf \"%.0f\" .Values.A.Value }} đơn hàng ghi thất bại trong 5 phút",
             "order-service không ghi được đơn vào PostgreSQL DBaaS – mất doanh thu trực tiếp. Kiểm tra kết nối DBaaS, pool, max_connections.",
             "lc-overview", keep_firing="5m"),
        rule("lc-inventory-rollback-fail", "Hoàn tồn kho thất bại (lệch tồn kho)",
             q_loki('sum(count_over_time({namespace=~"live-commerce.*", app="order-service"} |= "Hoàn tồn kho thất bại" [10m]))'),
             "gt", 0, "0s", "critical",
             "⚠️ Tồn kho có thể bị lệch: {{ printf \"%.0f\" .Values.A.Value }} lần hoàn kho thất bại",
             "Sau khi ghi đơn lỗi, order-service không trả lại tồn kho trên Redis. Cần đối soát tồn kho thủ công.",
             "lc-overview", keep_firing="15m"),
        rule("lc-ivs-errors", "Lỗi gọi AWS IVS",
             q_loki('sum(count_over_time({namespace=~"live-commerce.*", app="live-manager"} |~ "(PutMetadata|CreateStreamKey|ListStreams) lỗi|Không xóa được kênh" [10m]))'),
             "gt", 3, "0s", "warning",
             "live-manager gặp {{ printf \"%.0f\" .Values.A.Value }} lỗi AWS IVS trong 10 phút",
             "Có thể do throttling TPS của IVS, credential hết hạn hoặc VPN/Internet ra AWS gặp sự cố. Seller có thể không phát được.",
             "lc-overview"),
        # Mỗi phiên là một alert riêng (label session/shop/title lấy từ log moderation-service),
        # để vi phạm ở phiên khác không bị gộp vào alert đang firing.
        rule("lc-moderation-stop", "Phiên live bị dừng do vi phạm",
             q_loki('sum by (kind, violation, session, shop, title) (count_over_time('
                    '{namespace=~"moderation.*", app="moderation-service"} |= "STREAM_STOPPED" '
                    '| regexp `Moderation event \\S+: (?P<kind>[A-Z_]+)/\\S+ labels=(?P<violation>.*?) '
                    'session=(?P<session>\\S+) shop="(?P<shop>[^"]*)" title="(?P<title>[^"]*)"` '
                    '| session != "" [5m]))'),
             "gt", 0, "0s", "info",
             "🛑 Shop {{ or .Labels.shop \"(không rõ)\" }} – phiên «{{ or .Labels.title \"(không rõ)\" }}» bị dừng do {{ .Labels.kind }}",
             "Nhãn vi phạm: **{{ .Labels.violation }}**. Session `{{ .Labels.session }}`. "
             "moderation-service đã dừng stream. Đội vận hành nội dung cần xem lại.", "lc-overview", team="trust-safety"),
        rule("lc-error-log-spike", "Log lỗi tăng đột biến",
             q_loki('sum by (app) (count_over_time({namespace=~"live-commerce.*|moderation.*"} |~ "(?i)\\b(error|exception|fatal)\\b" [5m]))'),
             "gt", 50, "5m", "warning",
             "{{ .Labels.app }}: {{ printf \"%.0f\" .Values.A.Value }} dòng log lỗi/5m",
             "Số log lỗi vượt 50 dòng/5 phút liên tục.", "lc-overview"),
    ]})

    # ---------- Hạ tầng ----------
    nodes = 'node_name=~".+"'
    groups.append({"orgId": 1, "name": "lc-infra", "folder": "Live Commerce", "interval": "1m", "rules": [
        rule("lc-node-notready", "Node NotReady",
             q_prom('sum by (node) (kube_node_status_condition{condition="Ready", status!="true"})'),
             "gt", 0, "3m", "critical",
             "Node {{ .Labels.node }} NotReady",
             "Pod trên node sẽ bị evict sau ~5 phút. Kiểm tra VM trên portal CMC Cloud.", "lc-cluster"),
        rule("lc-node-cpu", "Node CPU cao",
             q_prom(f'1 - avg by (node_name) (rate(node_cpu_seconds_total{{mode="idle", {nodes}}}[5m]))'),
             "gt", 0.85, "10m", "warning",
             "{{ .Labels.node_name }} CPU {{ humanizePercentage .Values.A.Value }}", "CPU node > 85 % trong 10 phút.", "lc-cluster"),
        rule("lc-node-mem", "Node memory cao",
             q_prom(f'1 - node_memory_MemAvailable_bytes{{{nodes}}} / node_memory_MemTotal_bytes{{{nodes}}}'),
             "gt", 0.9, "10m", "critical",
             "{{ .Labels.node_name }} memory {{ humanizePercentage .Values.A.Value }}",
             "Memory node > 90 %, kubelet sắp evict pod.", "lc-cluster"),
        rule("lc-node-disk", "Node đĩa / sắp đầy",
             q_prom(f'1 - node_filesystem_avail_bytes{{mountpoint="/", {nodes}}} / node_filesystem_size_bytes{{mountpoint="/", {nodes}}}'),
             "gt", 0.85, "10m", "warning",
             "{{ .Labels.node_name }} đĩa / dùng {{ humanizePercentage .Values.A.Value }}",
             "Khi > 90 % kubelet sẽ GC image và evict pod (DiskPressure).", "lc-cluster"),
        rule("lc-node-disk-predict", "Node đĩa / sẽ đầy trong 24h",
             q_prom(f'predict_linear(node_filesystem_avail_bytes{{mountpoint="/", {nodes}}}[6h], 24 * 3600)', rng=21600),
             "lt", 0, "30m", "warning",
             "{{ .Labels.node_name }} dự báo hết đĩa trong 24h", "Theo xu hướng 6h gần nhất.", "lc-cluster"),
        rule("lc-cluster-cpu-req", "Cluster gần hết CPU để schedule",
             q_prom('sum(kube_pod_container_resource_requests{resource="cpu", node=~".*-worker-.*"}) / sum(kube_node_status_allocatable{resource="cpu", node=~".*-worker-.*"})'),
             "gt", 0.85, "15m", "warning",
             "CPU request đã dùng {{ humanizePercentage .Values.A.Value }} allocatable của worker",
             "HPA sẽ tạo pod Pending khi có phiên live lớn. Thêm node vào node group.", "lc-cluster"),
        # Pod không có chỗ chạy: dấu hiệu Cluster Autoscaler cần thêm node (hoặc nodegroup đã ở maxSize).
        rule("lc-pod-unschedulable", "Pod Pending vì cluster hết chỗ",
             q_prom(f'sum by (namespace) (kube_pod_status_unschedulable{{{ns}}})'),
             "gt", 0, "1m", "warning",
             "{{ .Labels.namespace }}: {{ printf \"%.0f\" .Values.A.Value }} pod Pending vì không node nào đủ CPU/memory",
             "Cluster Autoscaler của CMC sẽ thêm node trong khoảng 3-5 phút. Nếu vẫn Pending, nodegroup đã ở maxSize.",
             "lc-cluster"),
        # Node tạo trong 15 phút gần nhất: Cluster Autoscaler scale up hoặc auto-healing thay node chết.
        rule("lc-node-joined", "Node mới tham gia cluster",
             q_prom('count by (node) (time() - kube_node_created{node=~".*-worker-.*"} < 900)'),
             "gt", 0, "0s", "info",
             "Node mới {{ .Labels.node }} đã tham gia cluster",
             "Do Cluster Autoscaler scale up hoặc auto-healing của CMC thay node hỏng. Kiểm tra harbor-ca-installer đã chạy trên node mới.",
             "lc-cluster"),
        rule("lc-target-down", "Mất scrape metrics (node-exporter / kube-state-metrics / kubelet)",
             q_prom('sum by (job) (up{job!="kubernetes-apiservers"} == 0)'),
             "gt", 0, "5m", "warning",
             "Job {{ .Labels.job }} có {{ printf \"%.0f\" .Values.A.Value }} target down",
             "Prometheus CMC không scrape được – dashboard và alert khác có thể mù. Báo đội CMC nếu kéo dài.", "lc-cluster"),
        rule("lc-apiserver-down", "API server không phản hồi",
             q_prom('count(up{job="kubernetes-apiservers", instance=~".*:6443"} == 0)'),
             "gt", 0, "3m", "critical",
             "{{ printf \"%.0f\" .Values.A.Value }} API server down", "Control plane do CMC quản lý – mở ticket CMC.", "lc-cluster"),
        rule("lc-ksm-absent", "kube-state-metrics mất dữ liệu",
             q_prom('absent(kube_deployment_spec_replicas)'),
             "gt", 0, "5m", "critical",
             "Không còn metric kube-state-metrics",
             "Hầu hết alert workload sẽ không hoạt động. Kiểm tra pod kube-state-metrics ở ns cmc-monitoring.", "lc-cluster",
             no_data="OK"),
    ]})
    for g in groups:
        for r in g["rules"]:
            if r["uid"] in ROBUSTA_RULES:
                r["labels"]["route"] = "robusta"
    # Provisioning không tự xóa rule đã bỏ khỏi file: phải liệt kê ở đây.
    return {"apiVersion": 1, "groups": groups,
            "deleteRules": [{"orgId": 1, "uid": uid} for uid in ("lc-pod-crashloop", "lc-pod-oom")]}


# Alert pod/deployment: gửi qua Robusta (đính kèm log, event, runbook), KHÔNG gửi thẳng Discord.
# Pod crash và OOMKilled do Robusta tự bắt từ Kubernetes (on_pod_crash_loop / on_pod_oom_killed), không có rule Grafana.
ROBUSTA_RULES = {"lc-deploy-unavailable", "lc-deploy-down", "lc-container-waiting",
                 "lc-hpa-maxed", "lc-cpu-throttle", "lc-mem-near-limit"}


def contact_points():
    title = '{{ template "lc.discord.title" . }}'
    msg = '{{ template "lc.discord.message" . }}'

    # Tag role (không dùng @here): chỉ người on-call / T&S bị ping, kể cả khi người khác đọc được kênh.
    def ping(role_env):
        return '{{ if eq .Status "firing" }}<@&${%s}>\n{{ end }}' % role_env + msg

    cps = []
    for name, env, message in (
            ("discord-critical", "DISCORD_WEBHOOK_CRITICAL", ping("DISCORD_ROLE_ONCALL")),
            ("discord-warning", "DISCORD_WEBHOOK_WARNING", msg),
            ("discord-trust-safety", "DISCORD_WEBHOOK_TRUST_SAFETY", ping("DISCORD_ROLE_TRUST_SAFETY"))):
        cps.append({"orgId": 1, "name": name, "receivers": [{
            "uid": name, "type": "discord", "disableResolveMessage": False,
            "settings": {"url": "${%s}" % env, "title": title, "message": message, "use_discord_username": False}}]})
    cps.append({"orgId": 1, "name": "robusta-rca", "receivers": [{
        "uid": "robusta-rca", "type": "webhook", "disableResolveMessage": False,
        "settings": {"url": "http://robusta-runner.robusta.svc.cluster.local:80/api/alerts"}}]})
    # File này chứa webhook (bí mật) → render bằng envsubst rồi đóng vào Secret, xem README.
    return {"apiVersion": 1, "contactPoints": cps}


def templates():
    return {"apiVersion": 1, "templates": [{"orgId": 1, "name": "lc-discord", "template": DISCORD_TEMPLATE}]}


DISCORD_TEMPLATE = r'''{{ define "lc.discord.title" -}}
{{ if eq .Status "firing" }}{{ if eq .CommonLabels.severity "critical" }}🔴{{ else if eq .CommonLabels.severity "warning" }}🟠{{ else }}🔵{{ end }} [{{ .CommonLabels.severity | toUpper }}]{{ else }}✅ [RESOLVED]{{ end }} {{ .CommonLabels.alertname }}{{ if gt (len .Alerts.Firing) 1 }} ×{{ len .Alerts.Firing }}{{ end }}
{{- end }}

{{ define "lc.discord.alert" -}}
**{{ or .Annotations.summary .Labels.alertname }}**
{{ with .Annotations.description }}{{ . }}
{{ end -}}
{{ with .Labels.namespace }}• Namespace: `{{ . }}`
{{ end -}}
{{ with .Labels.deployment }}• Deployment: `{{ . }}`
{{ end -}}
{{ with .Labels.pod }}• Pod: `{{ . }}`
{{ end -}}
{{ with .Labels.svc }}• Service: `{{ . }}`
{{ end -}}
{{ with .Labels.node }}• Node: `{{ . }}`
{{ end -}}
{{ with .Labels.node_name }}• Node: `{{ . }}`
{{ end -}}
• Bắt đầu: <t:{{ .StartsAt.Unix }}:R>{{ if eq .Status "resolved" }} · Hết: <t:{{ .EndsAt.Unix }}:R>{{ end }}
{{ with .DashboardURL }}[📊 Dashboard]({{ . }}){{ end }}{{ with .PanelURL }} · [Panel]({{ . }}){{ end }}{{ with .Annotations.runbook_url }} · [📕 Runbook]({{ . }}){{ end }}{{ if eq .Status "firing" }} · [🔕 Silence]({{ .SilenceURL }}){{ end }}
{{- end }}

{{ define "lc.discord.message" -}}
{{ range .Alerts.Firing }}{{ template "lc.discord.alert" . }}

{{ end -}}
{{ range .Alerts.Resolved }}{{ template "lc.discord.alert" . }}

{{ end -}}
{{- end }}'''


def policies():
    return {"apiVersion": 1, "policies": [{
        "orgId": 1, "receiver": "discord-warning",
        "group_by": ["grafana_folder", "alertname", "namespace"],
        "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        "routes": [
            # Alert pod/deployment (ROBUSTA_RULES): Robusta gửi Discord kèm log/event/runbook, tránh 2 tin trùng.
            {"receiver": "robusta-rca", "object_matchers": [["route", "=", "robusta"]],
             "group_by": ["alertname", "namespace"], "group_wait": "10s", "group_interval": "2m",
             "repeat_interval": "30m", "continue": False},
            # Moderation: về kênh Trust & Safety, không làm ồn kênh on-call.
            # group theo session: mỗi phiên bị dừng là một tin Discord riêng, gửi gần như ngay.
            {"receiver": "discord-trust-safety", "object_matchers": [["team", "=", "trust-safety"]],
             "group_by": ["grafana_folder", "alertname", "session"], "group_wait": "10s",
             "repeat_interval": "4h", "continue": False},
            {"receiver": "discord-critical", "object_matchers": [["severity", "=", "critical"]],
             "group_wait": "10s", "group_interval": "2m", "repeat_interval": "30m", "continue": False},
            {"receiver": "discord-warning", "object_matchers": [["severity", "=~", "warning|info"]],
             "repeat_interval": "4h"},
        ]}]}


# Panel mà link "Panel" trong tin Discord trỏ tới (Grafana 12 bắt buộc có __panelId__ khi có __dashboardUid__).
# Tra theo tiêu đề panel lúc sinh file nên không lệch khi thêm/bớt panel.
RULE_PANEL = {
    "lc-deploy-unavailable": "Replica unavailable theo thời gian",
    "lc-deploy-down": "Replica unavailable theo thời gian",
    "lc-pod-crashloop": "Restart (tăng trong 5m)",
    "lc-pod-oom": "OOM events",
    "lc-container-waiting": "Container đang Waiting / lần chết gần nhất",
    "lc-hpa-maxed": "HPA",
    "lc-cpu-throttle": "CPU throttling %",
    "lc-mem-near-limit": "Memory % so với limit (OOMKill ở 100 %)",
    "lc-kong-5xx-ratio": "Tỷ lệ 5xx theo service",
    "lc-kong-upstream-down": "Request/s theo nhóm status",
    "lc-order-write-fail": "Lỗi nghiệp vụ đơn hàng (log order-service)",
    "lc-inventory-rollback-fail": "Lỗi nghiệp vụ đơn hàng (log order-service)",
    "lc-ivs-errors": "Phiên live mở & lỗi AWS IVS",
    "lc-moderation-stop": "Sự kiện kiểm duyệt (moderation)",
    "lc-error-log-spike": "Log lỗi / phút theo service",
    "lc-node-notready": "Node NotReady",
    "lc-node-cpu": "CPU % theo node",
    "lc-node-mem": "Memory % theo node",
    "lc-node-disk": "Đĩa / dùng % (+ dự báo đầy)",
    "lc-node-disk-predict": "Đĩa / dùng % (+ dự báo đầy)",
    "lc-cluster-cpu-req": "CPU requested / allocatable",
    "lc-pod-unschedulable": "Pod / capacity",
    "lc-node-joined": "Node Ready",
    "lc-target-down": "Scrape target theo job",
    "lc-apiserver-down": "API server up",
    "lc-ksm-absent": "Scrape target theo job",
}


def robusta_playbooks(rules):
    """customPlaybooks của Robusta (helm values). Robusta Discord không hiện annotation runbook_url thành nút,
    nên mỗi alert một playbook gắn đúng link runbook. Trigger khớp theo tên alert (title rule Grafana)."""
    sink = ["discord_critical_sink"]
    pod = [{"pod_events_enricher": {}}, {"pod_issue_investigator": {}}]
    deploy = [{"deployment_status_enricher": {}}, {"deployment_events_enricher": {}}, {"pod_issue_investigator": {}}]
    enrich = {"lc-deploy-unavailable": deploy, "lc-deploy-down": deploy, "lc-container-waiting": pod,
              "lc-cpu-throttle": pod, "lc-mem-near-limit": pod, "lc-hpa-maxed": []}

    def runbook(uid, title):
        return {"external_video_enricher": {"name": f"📕 Runbook: {title}", "url": f"{RB}#{uid}"}}

    pbs = [
        # Bắt trực tiếp từ Kubernetes (báo ngay, kèm log container trước khi chết).
        {"triggers": [{"on_pod_crash_loop": {"restart_count": 1, "rate_limit": 30}}],
         "actions": [runbook("lc-pod-crashloop", "Pod restart liên tục"), {"report_crash_loop": {}}], "sinks": sink},
        {"triggers": [{"on_pod_oom_killed": {"rate_limit": 60}}],
         "actions": [runbook("lc-pod-oom", "Container bị OOMKilled"), {"pod_oom_killer_enricher": {}}], "sinks": sink},
    ]
    for g in rules["groups"]:
        for r in g["rules"]:
            if r["uid"] in ROBUSTA_RULES:
                pbs.append({"triggers": [{"on_prometheus_alert": {"alert_name": r["title"]}}],
                            "actions": [runbook(r["uid"], r["title"])] + enrich[r["uid"]], "sinks": sink})
    return {"customPlaybooks": pbs}


def link_panels(rules, dashboards):
    for g in rules["groups"]:
        for r in g["rules"]:
            ann = r["annotations"]
            if "__dashboardUid__" not in ann or "__panelId__" in ann:
                continue
            panels = {p["title"]: p["id"] for p in dashboards[ann["__dashboardUid__"]]["panels"]}
            ann["__panelId__"] = str(panels[RULE_PANEL[r["uid"]]])
    return rules


def mark_no_traffic(d):
    """Panel Loki (Kong/app log) không có dòng log nào → Grafana hiện "No data", dễ nhầm là hỏng.
    Ghi rõ là không có traffic (stat hiện 0)."""
    for p in d["panels"]:
        if (p.get("datasource") or {}).get("type") != "loki" or p["type"] in ("row", "text", "logs"):
            continue
        p["fieldConfig"]["defaults"]["noValue"] = "0" if p["type"] == "stat" else "Không có request / log trong khoảng thời gian này"
    return d


def main():
    (ROOT / "dashboards").mkdir(exist_ok=True)
    (ROOT / "alerting").mkdir(exist_ok=True)
    dashboards = {"lc-overview": overview(), "lc-workloads": workloads(), "lc-cluster": cluster()}
    for name, d in dashboards.items():
        mark_no_traffic(d)
        (ROOT / "dashboards" / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")

    class D(yaml.SafeDumper):
        def ignore_aliases(self, data):   # không sinh &id001/*id001 khi dùng lại cùng dict/list
            return True

    def str_rep(dumper, s):
        style = "|" if "\n" in s else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)
    D.add_representer(str, str_rep)

    rules = link_panels(alert_rules(), dashboards)
    (ROOT.parent / "robusta").mkdir(exist_ok=True)
    (ROOT.parent / "robusta" / "playbooks.yaml").write_text(
        "# SINH TỰ ĐỘNG bởi grafana/gen.py – sửa gen.py rồi chạy lại. Áp dụng: xem README.md cùng thư mục.\n"
        + yaml.dump(robusta_playbooks(rules), Dumper=D, allow_unicode=True, sort_keys=False, width=200))
    for fname, obj in (("rules.yaml", rules), ("contact-points.tmpl.yaml", contact_points()),
                       ("templates.yaml", templates()),
                       ("policies.yaml", policies())):
        (ROOT / "alerting" / fname).write_text(
            "# SINH TỰ ĐỘNG bởi gen.py – sửa gen.py rồi chạy lại.\n"
            + yaml.dump(obj, Dumper=D, allow_unicode=True, sort_keys=False, width=200))
    print("ok")


if __name__ == "__main__":
    main()
