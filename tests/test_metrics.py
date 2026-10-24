# -*- coding: utf-8 -*-
"""指标格式与直方图的累积语义（接 Grafana / scrape 时最容易错的地方）。"""
from metrics import Metrics


def test_counter_with_labels():
    m = Metrics()
    m.inc("tasks_total", {"state": "succeeded"})
    m.inc("tasks_total", {"state": "succeeded"})
    m.inc("tasks_total", {"state": "failed"})
    text = m.render()
    assert 'tasks_total{state="failed"} 1' in text
    assert 'tasks_total{state="succeeded"} 2' in text
    assert "# TYPE tasks_total counter" in text


def test_gauge():
    m = Metrics()
    m.set_gauge("queue_depth", 3)
    assert "queue_depth 3" in m.render()
    assert "# TYPE queue_depth gauge" in m.render()


def test_histogram_is_cumulative():
    m = Metrics()
    m.observe("dur", 0.4)   # <=0.5
    m.observe("dur", 3.0)   # <=5
    text = m.render()
    # 0.5 桶只装第一个；5 桶装两个；+Inf 也是两个
    assert 'dur_bucket{le="0.5"} 1' in text
    assert 'dur_bucket{le="5"} 2' in text
    assert 'dur_bucket{le="+Inf"} 2' in text
    assert "dur_count 2" in text
    assert "dur_sum 3.4" in text


def test_snapshot_shape():
    m = Metrics()
    m.inc("a_total", {"k": "v"})
    m.set_gauge("g", 1)
    m.observe("h", 2)
    snap = m.snapshot()
    assert snap["counters"]['a_total{k="v"}'] == 1
    assert snap["gauges"]["g"] == 1
    assert snap["histograms"]["h"]["count"] == 1


def test_big_value_goes_to_inf_only():
    m = Metrics()
    m.observe("h", 99999)
    text = m.render()
    assert 'h_bucket{le="0.5"} 0' in text
    assert 'h_bucket{le="+Inf"} 1' in text
