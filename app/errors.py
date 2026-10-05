"""领域错误与统一错误响应。"""


class ApiError(Exception):
    """业务错误，携带 HTTP 状态码、机器可读错误码与详细信息。"""

    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ---- 常用构造器 ----------------------------------------------------------------

def unknown_node(node_id: str) -> ApiError:
    return ApiError(404, "NODE_NOT_FOUND", f"未知节点: {node_id}", {"node_id": node_id})


def tide_missing(missing_minutes) -> ApiError:
    missing = sorted(int(m) for m in missing_minutes)
    shown = missing[:20]
    return ApiError(
        422,
        "MISSING_TIDE_DATA",
        f"规划涉及的窗口内有 {len(missing)} 个分钟缺少潮位数据",
        {"missing_minutes": shown, "total_missing": len(missing)},
    )


def tide_non_finite(revision: int, non_finite_minutes,
                    fields=("level", "error")) -> ApiError:
    """已存修订在指定分钟含非有限潮位值（±∞/NaN，旧缺陷遗留），拒绝读取。

    历史修订不可变，异常值不被改写：凡查询/规划/确认/批次/复核改期需要该
    修订的这些分钟时，一律 422 拒绝（绝不按"安全"放行，也不回退 500）。
    以旧修订分叉提交有限新值覆盖后，新修订的该分钟恢复正常（见
    services/tide.py 的差量覆盖语义）。
    """
    minutes = sorted(int(m) for m in non_finite_minutes)
    return ApiError(
        422,
        "NON_FINITE_TIDE_DATA",
        f"潮位修订 #{revision} 有 {len(minutes)} 个分钟的名义潮位或预测误差"
        "不是有限数（±∞/NaN，旧版本缺陷遗留，历史修订不被改写）；"
        "请基于该修订分叉，以有限新值覆盖异常分钟后再使用",
        {"revision": revision,
         "non_finite_minutes": minutes[:20],
         "total_non_finite": len(minutes),
         "fields": sorted(fields)},
    )


def trip_not_found(trip_id: int) -> ApiError:
    return ApiError(404, "TRIP_NOT_FOUND", f"已确认行程不存在: {trip_id}",
                    {"trip_id": trip_id})


def no_itinerary(origin: str, destination: str) -> ApiError:
    return ApiError(
        404,
        "NO_FEASIBLE_ITINERARY",
        f"在给定窗口内不存在从 {origin} 到 {destination} 的可行行程",
        {"origin": origin, "destination": destination},
    )


def ship_not_found(ship_no: str) -> ApiError:
    return ApiError(404, "SHIP_NOT_FOUND", f"未知船号: {ship_no}",
                    {"ship_no": ship_no})


def berth_not_found(berth_id: str) -> ApiError:
    return ApiError(404, "BERTH_NOT_FOUND", f"未知泊位: {berth_id}",
                    {"berth_id": berth_id})


def berth_exists(berth_id: str) -> ApiError:
    return ApiError(409, "BERTH_EXISTS", f"泊位已存在: {berth_id}",
                    {"berth_id": berth_id})


def berth_not_at_destination(berth_id: str, berth_node: str,
                             destination: str) -> ApiError:
    return ApiError(
        422,
        "BERTH_NOT_AT_DESTINATION",
        f"泊位 {berth_id} 属于节点 {berth_node}，不属于终点 {destination}，拒绝预排",
        {"berth_id": berth_id, "berth_node": berth_node,
         "destination": destination},
    )
