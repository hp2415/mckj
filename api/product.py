from fastapi import APIRouter, HTTPException
import httpx

router = APIRouter(prefix="/api/product", tags=["Products"])

def parse_832_data(raw_json):
    """
    提取所需的商品字段，防止返回太多冗余数据
    """
    if "data" in raw_json and "list" in raw_json["data"]:
        products = raw_json["data"]["list"]
        result = []
        for p in products:
            result.append({
                "productId": p.get("productId"),
                "productName": p.get("productName"),
                "price": p.get("price"),
                "imagePath": p.get("imagePath"),
                "supplierName": p.get("supplierName")
            })
        return result
    return raw_json

@router.post("/search_832")
async def search_832_products(supplier_id: str, page: int = 1):
    url = "https://ys.fupin832.com/frontweb/search/searchProduct"
    headers = {
        "origin": "https://ys.fupin832.com",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    payload = {
        "nowPage": page,
        "pageShow": 20,
        "sortType": "DESC",
        "supplierId": supplier_id,
        "sortName": "",
        "shopCategoryId": ""
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status() 
            return parse_832_data(response.json())
            
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"外部接口返回错误: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"内部逻辑错误: {str(e)}")
