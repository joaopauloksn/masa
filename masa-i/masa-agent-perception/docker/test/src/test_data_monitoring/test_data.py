import data_monitoring.monitor as monitor


# def test_get_latency():
#     assert (
#         monitor.get_latency(metric="nonExistent", method="noRoute", time_frame="5m")
#         is None
#     )


# def test_get_latency_sample():
#     assert (
#         monitor.get_latency(
#             metric="microservices_demo_user_request_latency_microseconds_sum",
#             method="login",
#             time_frame="5m",
#         )
#         is not None
#     )


# def test_get_slo_percentage():
#     assert (
#         monitor.get_slo_percentage(
#             metric="nonExistent",
#             job="nonExistent",
#             route="noRoute",
#             status_code="200",
#             time_frame="5m",
#             quantile="2.5",
#         )
#         is None
#     )


# def test_get_slo_percentage_sample():
#     assert (
#         monitor.get_slo_percentage(
#             metric="request_duration_seconds",
#             job="user",
#             route="login",
#             status_code="200",
#             time_frame="5m",
#             quantile="2.5",
#         )
#         is not None
#     )


# def test_get_failure_rate():
#     assert (
#         monitor.get_failure_rate(
#             metric="nonExistent",
#             job="nonExistent",
#             route="noRoute",
#             status_code="200",
#             time_frame="5m",
#         )
#         is None
#     )


# def test_get_failure_rate_sample():
#     assert (
#         monitor.get_failure_rate(
#             metric="request_duration_seconds",
#             job="user",
#             route="login",
#             status_code="200",
#             time_frame="5m",
#         )
#         is not None
#     )


# def test_get_performance():
#     assert (
#         monitor.get_performance(
#             metric="request_duration_seconds",
#             job="nonExistent",
#             route="noRoute",
#             status_code="200",
#             time_frame="5m",
#             target="0.95",
#         )
#         is None
#     )


# def test_get_performance_sample():
#     assert (
#         monitor.get_performance(
#             metric="request_duration_seconds",
#             job="user",
#             route="login",
#             status_code="200",
#             time_frame="5m",
#             target="0.95",
#         )
#         is not None
#     )


# def test_get_get_qps():
#     assert (
#         monitor.get_qps(
#             metric="request_duration_seconds",
#             job="nonExistent",
#             route="noRoute",
#             status_code="2..",
#             time_frame="5m",
#         )
#         is None
#     )


# def test_get_get_qps_sample_success():
#     assert (
#         monitor.get_qps(
#             metric="request_duration_seconds",
#             job="user",
#             route="login",
#             status_code="2..",
#             time_frame="5m",
#         )
#         is not None
#     )


# def test_get_get_qps_sample_failure():
#     assert (
#         monitor.get_qps(
#             metric="request_duration_seconds",
#             job="user",
#             route="login",
#             status_code="4.+|5.+",
#             time_frame="5m",
#         )
#         is None
#     )


# def test_get_get_replicas():
#     assert monitor.get_replicas(namespace="nonExistent", deployment="none") is None


# def test_get_get_replicas_sample1():
#     assert monitor.get_replicas(namespace="scaling", deployment="user") is not None


# def test_get_get_replicas_sample2():
#     assert monitor.get_replicas(namespace="scaling", deployment="front-end") is not None


def test_full_log():
    userLogin = monitor.Microservice(
        name="login",
        performance_metric="request_duration_seconds",
        latency_metric="microservices_demo_user_request_latency_microseconds_sum",
        time_frame="5m",
        job="user",
        method="login",
        route="login",
        target="0.95",
        quantile="2.5",
        namespace="scaling",
        deployment="user"
    )
    time_series = monitor.get_metrics(userLogin)
    entry = monitor.get_time_series_entry(userLogin, time_series)
    monitor.persist_data(entry)


def test_label():
    time_series = {'latency': '0', 'slo_target': '0.95', 'slo_current': None, 'failure_rate': None, 'performance': None, 'qps_success': '0', 'qps_failure': None, 'replicas': '1'}
    label = monitor.get_label(time_series)
    assert label == 3
    time_series = {'latency': '0', 'slo_target': '0.95', 'slo_current': '0.9688', 'failure_rate': None, 'performance': None, 'qps_success': '0', 'qps_failure': None, 'replicas': '1'}
    label = monitor.get_label(time_series)
    assert label == 2
    time_series = {'latency': '0', 'slo_target': '0.95', 'slo_current': '0.9388', 'failure_rate': None, 'performance': None, 'qps_success': '0', 'qps_failure': None, 'replicas': '1'}
    label = monitor.get_label(time_series)
    assert label == 1
