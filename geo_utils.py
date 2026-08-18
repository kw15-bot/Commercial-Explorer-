"""
geo_utils.py
scraperとprovidersで共通に使う、依存ライブラリなしの簡易ジオメトリ関数群。
"""


def ring_bbox(ring):
    """[[lon,lat], ...] -> (west, south, east, north)"""
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    return min(lons), min(lats), max(lons), max(lats)


def point_in_ring(lon, lat, ring):
    """レイキャスティング法による簡易点内判定（凹多角形にも対応）。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def polygon_centroid(ring):
    """頂点平均による簡易重心（真の面積重心ではないが、フットプリント程度の
    小〜中規模な凸形状であれば絞り込み用途には十分な近似）。"""
    lon = sum(c[0] for c in ring) / len(ring)
    lat = sum(c[1] for c in ring) / len(ring)
    return lon, lat
