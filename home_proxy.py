import asyncio
import logging
from aiohttp import web
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

client = httpx.AsyncClient(verify=False, timeout=60.0)

async def handle_request(request: web.Request):
    # Read the original target host from X-Forwarded-Host (set by the Render server patch)
    original_host = request.headers.get('X-Forwarded-Host', 'gemini.google.com')
    url = f"https://{original_host}{request.path_qs}"
    logging.info(f"Reverse Proxying -> {request.method} {url}")
    
    # Forward headers but remove hop-by-hop and tunnel-specific ones
    headers = dict(request.headers)
    for h in ['Host', 'Connection', 'Content-Length', 'Transfer-Encoding',
              'Accept-Encoding',
              'X-Forwarded-Host', 'ngrok-skip-browser-warning',
              'ngrok-trace-id', 'x-forwarded-for', 'x-forwarded-proto']:
        headers.pop(h, None)
        headers.pop(h.lower(), None)
    
    headers['Host'] = original_host
    headers['Origin'] = f'https://{original_host}'
    if 'Referer' in headers:
        headers['Referer'] = headers['Referer'].replace(request.host, original_host)

    # Read body explicitly to pass to httpx
    body = await request.read()
    
    # Build httpx request
    proxy_request = client.build_request(
        request.method, url,
        headers=headers,
        content=body
    )
    
    # Send and stream the response back
    try:
        proxy_resp = await client.send(proxy_request, stream=True)
        
        resp_headers = dict(proxy_resp.headers)
        for h in ['Connection', 'Transfer-Encoding', 'Content-Length']:
            resp_headers.pop(h, None)
            resp_headers.pop(h.lower(), None)
            
        response = web.StreamResponse(
            status=proxy_resp.status_code,
            headers=resp_headers
        )
        
        await response.prepare(request)
        
        full_body = b"" if '/app' in request.path else None
        async for chunk in proxy_resp.aiter_bytes():
            if full_body is not None:
                full_body += chunk
            await response.write(chunk)
            
        await proxy_resp.aclose()
        
        if full_body is not None:
            text = full_body.decode('utf-8', errors='replace')
            has_snlm0e = 'SNlM0e' in text
            cookie_hdr = request.headers.get('Cookie', '')
            has_psid = '__Secure-1PSID' in cookie_hdr
            has_psidts = '__Secure-1PSIDTS' in cookie_hdr
            logging.info(
                f"[DEBUG /app] status={proxy_resp.status_code} body={len(full_body)}B "
                f"SNlM0e={'FOUND' if has_snlm0e else 'NOT FOUND'} "
                f"cookies_forwarded: 1PSID={has_psid} 1PSIDTS={has_psidts}"
            )
            if not has_snlm0e:
                # Show first 300 chars of <title> area or beginning
                idx = text.find('<title')
                snippet = text[idx:idx+200] if idx >= 0 else text[:300]
                logging.info(f"[DEBUG /app] snippet: {snippet}")
        
        return response
        
    except Exception as e:
        logging.error(f"Error reverse proxying {url}: {e}")
        return web.Response(status=502, text=str(e))

app = web.Application()
app.router.add_route('*', '/{path:.*}', handle_request)

if __name__ == '__main__':
    logging.info("Starting WebAI-to-API Reverse Proxy on 0.0.0.0:8888")
    web.run_app(app, host='0.0.0.0', port=8888)
