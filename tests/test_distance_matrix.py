import math

from routing.distance_matrix import (
    CHUNK_REQUEST_DELAY_SECONDS,
    _chunk_indices,
    DistanceMatrixBuilder,
    FALLBACK_SPEED_KMH,
    ROAD_INDIRECTNESS,
    haversine_km,
)


def test_haversine_km_same_point_is_zero():
    assert haversine_km((55.7, 37.6), (55.7, 37.6)) == 0.0


def test_haversine_km_known_distance_order_of_magnitude():
    # Красная площадь -> МГУ, примерно 8-9 км по прямой.
    km = haversine_km((55.7539, 37.6208), (55.7033, 37.5304))
    assert 7.0 < km < 10.0


def test_fallback_matrix_uses_indirectness_and_speed(tmp_path):
    builder = DistanceMatrixBuilder(
        cache_path=tmp_path / "cache.json", api_key=None, force_fallback=True
    )
    points = [(55.70, 37.60), (55.71, 37.61)]

    result = builder.build("foot", points)

    assert result.source == "fallback"
    assert result.distance_km[(0, 0)] == 0.0
    expected_km = haversine_km(points[0], points[1]) * ROAD_INDIRECTNESS
    assert math.isclose(result.distance_km[(0, 1)], expected_km, rel_tol=1e-9)
    expected_min = expected_km / FALLBACK_SPEED_KMH["foot"] * 60
    assert math.isclose(result.duration_min[(0, 1)], expected_min, rel_tol=1e-9)


def test_fallback_used_when_no_api_key(tmp_path, monkeypatch):
    # Явно убираем ORS_API_KEY из окружения: тест проверяет поведение при
    # ОТСУТСТВИИ ключа, а не полагается на то, что его случайно не оказалось
    # в окружении (например, после load_dotenv() из другого теста в этом же
    # процессе pytest — build_map.main() и run_distribute.main() оба зовут
    # load_dotenv(), которая мутирует настоящий os.environ).
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key=None)

    result = builder.build("car", [(55.70, 37.60), (55.71, 37.61)])

    assert result.source == "fallback"


def test_transit_profile_derives_from_foot(tmp_path):
    builder = DistanceMatrixBuilder(
        cache_path=tmp_path / "cache.json", api_key=None, force_fallback=True
    )
    points = [(55.70, 37.60), (55.71, 37.61)]

    foot_result = builder.build("foot", points)
    transit_result = builder.build("transit", points)

    assert transit_result.distance_km == foot_result.distance_km
    expected_min = foot_result.distance_km[(0, 1)] / FALLBACK_SPEED_KMH["transit"] * 60
    assert math.isclose(transit_result.duration_min[(0, 1)], expected_min, rel_tol=1e-9)


def test_ors_success_parses_response_and_caches(tmp_path, monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"distances": [[0.0, 3.2], [3.2, 0.0]], "durations": [[0.0, 600.0], [600.0, 0.0]]}

    def fake_post(url, json, headers, timeout):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    points = [(55.70, 37.60), (55.71, 37.61)]

    result = builder.build("car", points)
    assert result.source == "ors"
    assert result.distance_km[(0, 1)] == 3.2
    assert result.duration_min[(0, 1)] == 10.0

    # Второй вызов с теми же точками должен взяться из кэша, без нового HTTP-запроса.
    result2 = builder.build("car", points)
    assert result2.source == "cache"
    assert len(calls) == 1


def test_ors_failure_falls_back(tmp_path, monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise ConnectionError("нет сети")

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    result = builder.build("car", [(55.70, 37.60), (55.71, 37.61)])

    assert result.source == "fallback"


def test_ors_malformed_response_missing_durations_falls_back(tmp_path, monkeypatch):
    # HTTP 200 без ключа "durations" должен давать fallback, а не KeyError.
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"distances": [[0.0, 3.2], [3.2, 0.0]]}

    monkeypatch.setattr("requests.post", lambda url, json, headers, timeout: FakeResponse())
    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")

    result = builder.build("car", [(55.70, 37.60), (55.71, 37.61)])

    assert result.source == "fallback"


def test_ors_malformed_response_wrong_shape_falls_back(tmp_path, monkeypatch):
    # Матрица неожиданного размера должна давать fallback, а не IndexError.
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"distances": [[0.0]], "durations": [[0.0]]}

    monkeypatch.setattr("requests.post", lambda url, json, headers, timeout: FakeResponse())
    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")

    result = builder.build("car", [(55.70, 37.60), (55.71, 37.61)])

    assert result.source == "fallback"


def test_chunk_indices_splits_evenly():
    assert _chunk_indices(6, 3) == [[0, 1, 2], [3, 4, 5]]


def test_chunk_indices_last_block_shorter():
    assert _chunk_indices(7, 3) == [[0, 1, 2], [3, 4, 5], [6]]


def test_chunk_indices_single_block_when_n_fits():
    assert _chunk_indices(2, 59) == [[0, 1]]


def test_chunk_indices_empty_for_zero_points():
    assert _chunk_indices(0, 5) == []


def test_ors_chunks_large_point_set_and_assembles_full_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    calls = []

    class FakeResponse:
        def __init__(self, sources, destinations):
            self._sources = sources
            self._destinations = destinations

        def raise_for_status(self):
            pass

        def json(self):
            # Предсказуемое значение по глобальным индексам источника/назначения,
            # чтобы проверить, что сборка блоков в общий словарь не путает индексы.
            return {
                "distances": [
                    [float(10 * s + d) for d in self._destinations] for s in self._sources
                ],
                "durations": [
                    [float(600 * s + d) for d in self._destinations] for s in self._sources
                ],
            }

    def fake_post(url, json, headers, timeout):
        calls.append((tuple(json["sources"]), tuple(json["destinations"])))
        return FakeResponse(json["sources"], json["destinations"])

    monkeypatch.setattr("requests.post", fake_post)

    # 5 точек, max_routes_per_request=4 -> chunk_size=2 -> блоки [0,1],[2,3],[4]
    # -> 3x3=9 запросов, чтобы покрыть полную матрицу 5x5.
    builder = DistanceMatrixBuilder(
        cache_path=tmp_path / "cache.json", api_key="test-key", max_routes_per_request=4
    )
    points = [(55.70 + i * 0.001, 37.60 + i * 0.001) for i in range(5)]

    result = builder.build("car", points)

    assert result.source == "ors"
    assert len(calls) == 9
    for s in range(5):
        for d in range(5):
            assert result.distance_km[(s, d)] == float(10 * s + d)
            assert result.duration_min[(s, d)] == float(600 * s + d) / 60.0


def test_ors_chunk_failure_falls_back_entire_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    call_count = 0

    class FakeResponse:
        def __init__(self, sources, destinations):
            self._sources = sources
            self._destinations = destinations

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "distances": [[0.0] * len(self._destinations) for _ in self._sources],
                "durations": [[0.0] * len(self._destinations) for _ in self._sources],
            }

    def fake_post(url, json, headers, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ConnectionError("нет сети на втором блоке")
        return FakeResponse(json["sources"], json["destinations"])

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(
        cache_path=tmp_path / "cache.json", api_key="test-key", max_routes_per_request=4
    )
    points = [(55.70 + i * 0.001, 37.60 + i * 0.001) for i in range(5)]

    result = builder.build("car", points)

    assert result.source == "fallback"
    assert call_count == 2


def test_fetch_geometry_success_parses_and_caches(tmp_path, monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "features": [
                    {
                        "geometry": {
                            "coordinates": [
                                [37.60, 55.70],
                                [37.605, 55.703],
                                [37.61, 55.71],
                            ]
                        }
                    }
                ]
            }

    def fake_post(url, json, headers, timeout):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    points = [(55.70, 37.60), (55.71, 37.61)]

    geometry = builder.fetch_geometry("car", points)
    assert geometry == [(55.70, 37.60), (55.703, 37.605), (55.71, 37.61)]

    # Второй вызов с теми же точками — из кэша, без нового HTTP-запроса.
    geometry2 = builder.fetch_geometry("car", points)
    assert geometry2 == geometry
    assert len(calls) == 1


def test_fetch_geometry_transit_derives_from_foot(tmp_path, monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": [{"geometry": {"coordinates": [[37.60, 55.70], [37.61, 55.71]]}}]}

    def fake_post(url, json, headers, timeout):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    points = [(55.70, 37.60), (55.71, 37.61)]

    geometry = builder.fetch_geometry("transit", points)
    assert geometry == [(55.70, 37.60), (55.71, 37.61)]
    assert "foot-walking" in calls[0]


def test_fetch_geometry_force_fallback_returns_none_without_network(tmp_path, monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise AssertionError("не должно быть сетевых запросов при force_fallback=True")

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(
        cache_path=tmp_path / "cache.json", api_key="test-key", force_fallback=True
    )
    geometry = builder.fetch_geometry("car", [(55.70, 37.60), (55.71, 37.61)])

    assert geometry is None


def test_fetch_geometry_fewer_than_two_points_returns_none_without_network(tmp_path, monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise AssertionError("не должно быть сетевых запросов для <2 точек")

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")

    assert builder.fetch_geometry("car", []) is None
    assert builder.fetch_geometry("car", [(55.70, 37.60)]) is None


def test_fetch_geometry_network_error_returns_none(tmp_path, monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise ConnectionError("нет сети")

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    geometry = builder.fetch_geometry("car", [(55.70, 37.60), (55.71, 37.61)])

    assert geometry is None


def test_fetch_geometry_sleeps_only_between_real_requests(tmp_path, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": [{"geometry": {"coordinates": [[37.60, 55.70], [37.61, 55.71]]}}]}

    def fake_post(url, json, headers, timeout):
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    points = [(55.70, 37.60), (55.71, 37.61)]

    builder.fetch_geometry("car", points)
    assert sleep_calls == []  # первый реальный запрос — без паузы

    builder.fetch_geometry("bike", points)
    assert sleep_calls == [CHUNK_REQUEST_DELAY_SECONDS]  # второй реальный запрос — с паузой

    # Третий вызов с уже закэшированными точками (car) — не сетевой запрос, новой паузы нет.
    builder.fetch_geometry("car", points)
    assert sleep_calls == [CHUNK_REQUEST_DELAY_SECONDS]


def test_fetch_geometry_malformed_response_returns_none(tmp_path, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": []}

    def fake_post(url, json, headers, timeout):
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    builder = DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", api_key="test-key")
    geometry = builder.fetch_geometry("car", [(55.70, 37.60), (55.71, 37.61)])

    assert geometry is None
