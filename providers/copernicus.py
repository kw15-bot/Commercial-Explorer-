"""
Copernicus Data Space Ecosystem (CDSE) OData API クライアント。

検索: GET https://catalogue.dataspace.copernicus.eu/odata/v1/Products
      認証不要（公開カタログ検索）。ダウンロードにのみアクセストークンが必要だが、
      本ツールは現在ダウンロードを外部リンク方式（Copernicus Browser側で認証）に
      しているため、このモジュールは検索のみで完結する。

Copernicus Browser の画面スクレイピングは行わず、Browser自身が内部で
呼んでいるのと同じ公式カタログAPIを直接叩く。
"""
import logging
import requests

logger = logging.getLogger(__name__)

ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DOWNLOAD_BASE = "https://download.dataspace.copernicus.eu/odata/v1/Products"
BROWSER_BASE = "https://browser.dataspace.copernicus.eu"


def _wkt_polygon_from_bbox(bbox):
    w, s, e, n = bbox[:4]
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def _wkt_polygon_from_ring(ring):
    pts = ",".join(f"{lon} {lat}" for lon, lat in ring)
    return f"POLYGON(({pts}))"


def search(bbox, start_iso, end_iso, collections, top=100, timeout=30, aoi_ring=None, max_pages=5):
    """aoi_ring（[[lon,lat],...]の閉じたリング）を渡すと、bboxではなくその
    正確なポリゴンでintersects検索する。渡さない場合はbboxの矩形を使う。

    レスポンスに "@odata.nextLink" があれば、それに従って追加ページを取得する
    （1リクエストのtopには上限があるため、これがないと取りこぼす）。
    max_pages は無限ループ・過度なAPI呼び出しを防ぐための安全弁。"""
    poly = _wkt_polygon_from_ring(aoi_ring) if aoi_ring else _wkt_polygon_from_bbox(bbox)

    filters = [
        f"ContentDate/Start gt {start_iso}",
        f"ContentDate/Start lt {end_iso}",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{poly}')",
    ]
    if collections:
        coll_filter = " or ".join(f"Collection/Name eq '{c}'" for c in collections)
        filters.append(f"({coll_filter})")

    params = {
        "$filter": " and ".join(filters),
        "$orderby": "ContentDate/Start desc",
        "$top": top,
        "$expand": "Attributes",
    }

    all_products = []
    url = ODATA_URL
    request_params = params
    for _ in range(max_pages):
        resp = requests.get(url, params=request_params, headers={"Accept": "application/json"}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        all_products.extend(data.get("value", []))
        next_link = data.get("@odata.nextLink")
        if not next_link:
            break
        url = next_link
        request_params = None  # nextLinkは完全なURL（クエリ文字列込み）なのでparams不要

    return [normalize(p) for p in all_products]


def normalize(product):
    attrs = product.get("Attributes", []) or []

    def get_attr(name):
        for a in attrs:
            if a.get("Name") == name:
                return a.get("Value")
        return None

    product_id = product.get("Id")
    name = product.get("Name")

    # 運用上使うのは以下の3種類のみ:
    #   ・サムネイル表示 -> Assets配列中 Type が QUICKLOOK/THUMBNAIL のものの DownloadLink
    #                     （CDSE OData の Assets フィールド。実データでの型・キー名は
    #                     未確認のため、無ければ None を返すだけにして安全側に倒す）
    #   ・メタデータ確認 -> OData詳細URL（$expand=Attributes で返る内容そのものをJSONで確認できる）
    #   ・DLリンク       -> Copernicus Browserで開く（HANDOFF §3の設計判断:
    #                     ダウンロードは認証込みでBrowser側に任せる外部リンク方式のため）
    assets = product.get("Assets", []) or []
    thumbnail_url = next(
        (a.get("DownloadLink") for a in assets
         if a and str(a.get("Type", "")).upper() in ("QUICKLOOK", "THUMBNAIL") and a.get("DownloadLink")),
        None,
    )

    return {
        "source": "Copernicus",
        "id": product_id,
        "sensor": _resolve_sensor(name),
        "datetime": (product.get("ContentDate") or {}).get("Start"),
        "cloud_cover": get_attr("cloudCover"),
        "resolution": "-",
        "name": name,
        "thumbnail_url": thumbnail_url,
        "metadata_url": f"{ODATA_URL}({product_id})",
        "download_url": get_download_url(product_id),  # 要Bearerトークン（フロント側でOAuth取得して付与）
        "links": [
            {"label": "Copernicus Browserで開く", "url": f"{BROWSER_BASE}/?zoom=8&product={name}" if name else BROWSER_BASE},
        ],
        "footprint": product.get("GeoFootprint"),
        "raw": product,
    }


# プロダクト名の接頭辞（例: S1A, S2B, S5P...）から、UI側のSATELLITES一覧・
# 光学/SARフィルタが期待する正規のセンサー名（例: "Sentinel-1"）へのマッピング。
# ここに無い接頭辞（c_gls_... = Copernicus Global Land Service 等、衛星画像
# そのものではない補助プロダクト）は "-" を返し、呼び出し側で除外する。
_SENSOR_PREFIX_MAP = {
    "S1A": "Sentinel-1", "S1B": "Sentinel-1", "S1C": "Sentinel-1",
    "S2A": "Sentinel-2", "S2B": "Sentinel-2", "S2C": "Sentinel-2",
    "S3A": "Sentinel-3", "S3B": "Sentinel-3",
    "S5P": "Sentinel-5P",
    "S6A": "Sentinel-6", "S6B": "Sentinel-6",
}


def _resolve_sensor(name):
    prefix = (name or "").split("_")[0]
    return _SENSOR_PREFIX_MAP.get(prefix, "-")


def get_access_token(username, password, timeout=30):
    """現在は未使用（ダウンロードは外部リンク方式のため）。将来サーバー経由
    ダウンロードに切り替える場合のために残してある。"""
    if not username or not password:
        raise RuntimeError("COPERNICUS_USERNAME / COPERNICUS_PASSWORD が設定されていません")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "cdse-public",
            "username": username,
            "password": password,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_download_url(product_id):
    return f"{DOWNLOAD_BASE}({product_id})/$value"
