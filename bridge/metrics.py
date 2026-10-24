# -*- coding: utf-8 -*-
"""极简进程内指标，输出 Prometheus 文本格式。

为什么自己写而不是上 prometheus_client：
- 只依赖标准库，免安装；本项目就 4 个指标，不需要完整客户端；
- 但保持 Prometheus 的命名/类型约定（_total / _seconds / histogram），
  以后接真实 scrape 或换成官方库都不用改采集端。

线程安全：看板在独立线程里读，runner 在事件循环里写，所以全部加锁。
"""
import threading
from collections import defaultdict

BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800)


def _label_str(labels):
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(float)      # (name, labels_tuple) -> value
        self._gauges = {}                        # name -> value
        self._hist = defaultdict(lambda: [0] * (len(BUCKETS) + 1))  # +Inf 槽
        self._hist_sum = defaultdict(float)
        self._hist_count = defaultdict(int)
        self._help = {
            "qq_remote_tasks_total": "任务计数，按最终状态",
            "qq_remote_task_errors_total": "错误计数，按错误类型",
            "qq_remote_task_duration_seconds": "任务执行时长直方图",
            "qq_remote_queue_depth": "当前排队任务数",
            "qq_remote_running": "当前执行中任务数",
            "qq_remote_cost_usd_total": "累计花费（美元，CLI 估算值，仅供参考）",
            "qq_remote_tokens_total": "token 用量，按方向（in 含缓存命中）",
        }

    # ---------- 写 ----------

    def inc(self, name, labels=None, v=1.0):
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] += v

    def set_gauge(self, name, v):
        with self._lock:
            self._gauges[name] = float(v)

    def observe(self, name, seconds):
        with self._lock:
            slot = self._hist[name]
            # 找到第一个 >= seconds 的桶；从它开始往后（含 +Inf）各 +1，
            # 这就是 Prometheus histogram 的 le（小于等于）累积语义。
            i = len(BUCKETS)
            for idx, b in enumerate(BUCKETS):
                if seconds <= b:
                    i = idx
                    break
            for j in range(i, len(slot)):
                slot[j] += 1
            self._hist_sum[name] += seconds
            self._hist_count[name] += 1

    # ---------- 读 ----------

    def snapshot(self):
        with self._lock:
            return {
                "counters": {f"{n}{_label_str(dict(l))}": v for (n, l), v in self._counters.items()},
                "gauges": dict(self._gauges),
                "histograms": {n: {"count": self._hist_count[n], "sum": self._hist_sum[n]}
                               for n in self._hist},
            }

    def render(self):
        """Prometheus 文本格式。"""
        out = []
        with self._lock:
            by_name = defaultdict(list)
            for (name, labels), v in self._counters.items():
                by_name[name].append((dict(labels), v))
            for name in sorted(by_name):
                mtype = "counter"
                out.append(f"# HELP {name} {self._help.get(name, name)}")
                out.append(f"# TYPE {name} {mtype}")
                for labels, v in by_name[name]:
                    out.append(f"{name}{_label_str(labels)} {v:g}")

            for name in sorted(self._gauges):
                out.append(f"# HELP {name} {self._help.get(name, name)}")
                out.append(f"# TYPE {name} gauge")
                out.append(f"{name} {self._gauges[name]:g}")

            for name in sorted(self._hist):
                out.append(f"# HELP {name} {self._help.get(name, name)}")
                out.append(f"# TYPE {name} histogram")
                slot = self._hist[name]
                for i, b in enumerate(BUCKETS):
                    out.append(f'{name}_bucket{{le="{b:g}"}} {slot[i]}')
                out.append(f'{name}_bucket{{le="+Inf"}} {slot[-1]}')
                out.append(f"{name}_sum {self._hist_sum[name]:g}")
                out.append(f"{name}_count {self._hist_count[name]}")
        return "\n".join(out) + "\n"


METRICS = Metrics()
