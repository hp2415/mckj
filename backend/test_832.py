import asyncio
import httpx
import json

async def test_832():
    url = "https://ys.fupin832.com/frontweb/search/searchProduct"
    headers = {
        "origin": "https://ys.fupin832.com",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = {
        "nowPage": 1,
        "pageShow": 10,
        "sortType": "DESC",
        "supplierId": "1090698369754404144",
        "sortName": "",
        "shopCategoryId": ""
    }
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        data = resp.json()
        print("======== 原始数据 ========")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:1000]) # 截断打印防止过长
        print("=========================")
        
        list_data = data.get("data", {}).get("list", [])
        print(f"提取到了 {len(list_data)} 条 list 数据！")

if __name__ == "__main__":
    asyncio.run(test_832())
