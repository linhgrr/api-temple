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
    
    # Forward headers but remove hop-by-hop ones
    headers = dict(request.headers)
    for h in ['Host', 'Connection', 'Content-Length', 'Transfer-Encoding', 'X-Forwarded-Host',
              'ngrok-skip-browser-warning', 'ngrok-trace-id', 'x-forwarded-for', 'x-forwarded-proto']:
        headers.pop(h, None)
        headers.pop(h.lower(), None)
    
    # We must explicitly set host to the original target
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
        
        # Prepare aiohttp response
        resp_headers = dict(proxy_resp.headers)
        for h in ['Connection', 'Transfer-Encoding', 'Content-Encoding', 'Content-Length']:
            resp_headers.pop(h, None)
            resp_headers.pop(h.lower(), None)
            
        response = web.StreamResponse(
            status=proxy_resp.status_code,
            headers=resp_headers
        )
        
        # We tell aiohttp server we are going to start pumping chunks
        await response.prepare(request)
        
        async for chunk in proxy_resp.aiter_bytes():
            await response.write(chunk)
            
        await proxy_resp.aclose()
        return response
        
    except Exception as e:
        logging.error(f"Error reverse proxying {url}: {e}")
        return web.Response(status=502, text=str(e))

app = web.Application()
app.router.add_route('*', '/{path:.*}', handle_request)

if __name__ == '__main__':
    logging.info("Starting WebAI-to-API Reverse Proxy on 0.0.0.0:8888")
    web.run_app(app, host='0.0.0.0', port=8888)
