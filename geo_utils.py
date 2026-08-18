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


def split_bbox_grid(bbox, cols, rows):
    """(w,s,e,n) のbboxを cols×rows のグリッドに分割し、各セルのbboxを返す。

    MGP Discovery API等、1回のクエリで返せる件数に事実上の上限がある
    APIに対して、広域AOIを一括で投げると上限に達して取りこぼしが発生する
    （しかもエラーにならず静かに欠落する）ため、地理的に分割して個々の
    クエリが扱う件数を抑える目的で使う。"""
    w, s, e, n = bbox
    dw = (e - w) / cols
    dh = (n - s) / rows
    cells = []
    for r in range(rows):
        for c in range(cols):
            cell_w = w + c * dw
            cell_e = w + (c + 1) * dw
            cell_s = s + r * dh
            cell_n = s + (r + 1) * dh
            cells.append((cell_w, cell_s, cell_e, cell_n))
    return cells
