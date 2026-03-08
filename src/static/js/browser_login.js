// src/static/js/browser_login.js

class BrowserLogin {
    constructor() {
        this.btnStart = document.getElementById("btn-browser-login-start");
        this.btnStop = document.getElementById("btn-browser-login-stop");
        this.wrapper = document.getElementById("browser-login-wrapper");
        this.canvas = document.getElementById("browser-canvas");
        this.ctx = this.canvas ? this.canvas.getContext("2d") : null;
        this.statusEl = document.getElementById("browser-login-status");
        
        this.ws = null;
        this.isActive = false;

        if (this.btnStart) {
            this.btnStart.addEventListener("click", () => this.startSession());
        }
        if (this.btnStop) {
            this.btnStop.addEventListener("click", () => this.stopSession());
        }
        
        this.setupEventListeners();
    }

    setupEventListeners() {
        if (!this.canvas) return;

        // Mouse click
        this.canvas.addEventListener("click", (e) => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
            
            const rect = this.canvas.getBoundingClientRect();
            // Calculate scale because canvas might be CSS resized
            const scaleX = this.canvas.width / rect.width;
            const scaleY = this.canvas.height / rect.height;
            
            const x = (e.clientX - rect.left) * scaleX;
            const y = (e.clientY - rect.top) * scaleY;
            
            this.ws.send(JSON.stringify({
                type: "click",
                x: x,
                y: y
            }));
            
            // Refocus canvas to ensure keystrokes are captured
            this.canvas.focus();
        });

        // Keyboard press
        this.canvas.addEventListener("keydown", (e) => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
            
            // Prevent default scrolling when pressing space/arrows
            if(["Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].indexOf(e.code) > -1) {
                e.preventDefault();
            }
            
            // Send special keys or characters
            let keyToSend = e.key;
            if (e.key === "Enter") keyToSend = "Enter";
            else if (e.key === "Backspace") keyToSend = "Backspace";
            else if (e.key === "Tab") keyToSend = "Tab";
            else if (e.key.length === 1) keyToSend = e.key; // Single character
            
            this.ws.send(JSON.stringify({
                type: "keydown",
                key: keyToSend
            }));
        });
    }

    setStatus(html, isError = false) {
        if (!this.statusEl) return;
        this.statusEl.innerHTML = html;
        this.statusEl.className = "inline-result " + (isError ? "error" : "success");
    }

    async startSession() {
        if (this.isActive) return;
        
        this.btnStart.disabled = true;
        this.setStatus("Starting browser session... Please wait.", false);
        
        try {
            const data = await api.post("/api/admin/browser-login/start", {});
            
            if (data.success) {
                this.isActive = true;
                this.wrapper.classList.remove("hidden");
                this.btnStart.textContent = "Browser Running";
                this.setStatus("Connecting to stream...", false);
                this.connectWebSocket();
            } else {
                this.btnStart.disabled = false;
                this.setStatus(data.message || "Failed to start", true);
            }
        } catch (err) {
            this.btnStart.disabled = false;
            this.setStatus(err.message, true);
        }
    }

    connectWebSocket() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/api/admin/browser-login/ws`;
        
        this.ws = new WebSocket(wsUrl);
        // Request binary data as Blob
        this.ws.binaryType = 'blob';
        
        this.ws.onopen = () => {
            this.setStatus("Live stream connected. You can click and type.", false);
            this.canvas.focus();
        };
        
        this.ws.onmessage = async (event) => {
            if (event.data instanceof Blob) {
                // It's a screenshot (JPEG)
                const img = new Image();
                const url = URL.createObjectURL(event.data);
                
                img.onload = () => {
                    if (this.ctx) {
                        this.ctx.drawImage(img, 0, 0, this.canvas.width, this.canvas.height);
                    }
                    URL.revokeObjectURL(url);
                };
                img.src = url;
            }
        };
        
        this.ws.onclose = () => {
            if (this.isActive) {
                this.setStatus("Stream disconnected.", true);
            }
        };
        
        this.ws.onerror = (err) => {
            console.error("WS Error:", err);
        };
    }

    async stopSession() {
        this.btnStop.disabled = true;
        this.btnStop.textContent = "Extracting...";
        this.setStatus("Extracting cookies and verifying...", false);
        
        try {
            const data = await api.post("/api/admin/browser-login/stop", {});
            
            this.isActive = false;
            this.wrapper.classList.add("hidden");
            this.btnStart.disabled = false;
            this.btnStart.textContent = "Start Browser Session";
            this.btnStop.disabled = false;
            this.btnStop.textContent = "Extract Cookies & Close";
            
            if (this.ws) {
                this.ws.close();
                this.ws = null;
            }
            
            if (data.cookies_found) {
                this.setStatus(data.message, !data.success);
                // Trigger config reload in the UI if possible
                setTimeout(() => window.location.reload(), 2000);
            } else {
                this.setStatus(data.message || "Failed to extract cookies.", true);
            }
            
        } catch (err) {
            this.btnStop.disabled = false;
            this.btnStop.textContent = "Extract Cookies & Close";
            this.setStatus(err.message, true);
        }
    }
}

// Initialize when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
    window.browserLoginController = new BrowserLogin();
});
