import asyncio
import json
import websockets
from BinomoAPI.api import BinomoAPI

# CONFIGURACIÓN
ASSET = "EURUSD"
DEVICE_ID = "1b6290ce761c82f3a97189d35d2ed138"

async def deep_scan():
    print(f"🕵️  DEEP SCAN: Buscando velas en cualquier lugar...")
    print("------------------------------------------------")
    
    # 1. LOGIN
    email = input("Email: ")
    password = input("Password: ")
    api = BinomoAPI.login(email, password)
    
    if not api or not api.authtoken:
        print("❌ Login fallido")
        return

    # 2. HEADERS
    cookie_str = f"authtoken={api.authtoken}; device_id={DEVICE_ID}; device_type=web"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Origin": "https://binomo.com",
        "Cookie": cookie_str,
        "authorization-token": api.authtoken,
        "device-id": DEVICE_ID,
        "device-type": "web"
    }

    uri = "wss://ws.binomo.com/?v=2&vsn=2.0.0"

    print(f"🔌 Conectando...")

    async with websockets.connect(uri, extra_headers=headers) as ws:
        print("✅ Conectado. Enviando suscripciones agresivas...")
        
        # Enviar todo junto para provocar respuesta masiva
        await ws.send(json.dumps({"topic": "connection", "event": "phx_join", "payload": {}, "ref": "1", "join_ref": "1"}))
        
        # Suscripción al ACTIVO con rates_required (AQUÍ suele venir la data)
        await ws.send(json.dumps({
            "topic": f"asset:{ASSET}", "event": "phx_join", "payload": {"rates_required": True}, "ref": "2", "join_ref": "2"
        }))
        
        # Suscripción al STREAM
        await ws.send(json.dumps({
            "topic": f"range_stream:{ASSET}", "event": "phx_join", "payload": {}, "ref": "3", "join_ref": "3"
        }))

        print("⏳ Analizando CADA mensaje entrante por 10 segundos...")
        print("   (Buscando listas o diccionarios grandes)")

        start_time = asyncio.get_event_loop().time()
        
        found = False

        while True:
            if asyncio.get_event_loop().time() - start_time > 10:
                print("\n❌ FIN DEL ESCANEO. No se detectaron paquetes grandes.")
                break

            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(msg)
                
                event = data.get('event')
                payload = data.get('payload')
                topic = data.get('topic')
                
                # --- DETECTOR UNIVERSAL ---
                
                is_history = False
                candidate_data = None
                source = "Desconocido"

                # CASO 1: Lista directa en payload (s0, candles)
                if isinstance(payload, list) and len(payload) > 5:
                    is_history = True
                    candidate_data = payload
                    source = f"Evento '{event}' en '{topic}'"

                # CASO 2: Data dentro de phx_reply (respuesta a suscripción)
                elif event == "phx_reply" and isinstance(payload, dict):
                    response = payload.get('response')
                    if isinstance(response, list) and len(response) > 5:
                        is_history = True
                        candidate_data = response
                        source = f"Respuesta a suscripción ({topic})"
                    elif isinstance(response, dict) and 'rates' in response:
                        is_history = True
                        candidate_data = response['rates']
                        source = "Respuesta con key 'rates'"
                
                # --- REPORTE ---
                if is_history:
                    print("\n" + "█"*60)
                    print(f"🎉 ¡ENCONTRADO! Fuente: {source}")
                    print(f"📦 Cantidad de items: {len(candidate_data)}")
                    print("█"*60)
                    
                    first_item = candidate_data[0]
                    print(f"\n🔍 TIPO DE ITEM: {type(first_item)}")
                    print("📄 ESTRUCTURA REAL (COPIA ESTO):")
                    print(json.dumps(first_item, indent=4))
                    
                    found = True
                    return

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error: {e}")
                break

if __name__ == "__main__":
    asyncio.run(deep_scan())