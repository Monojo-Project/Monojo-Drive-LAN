#!/usr/bin/env python3

import os
import json
import socket
import struct
import threading
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

SETTINGS_FILE = "settings.json"
PUERTO_DESCUBRIMIENTO = 64000   # UDP discovery (probe/respuesta + anuncios)
PUERTO_CONTROL = 64001         # TCP control/transferencias
INTERVALO_ANNOUNCE = 2.0
TAM_BUFFER = 64 * 1024
DISCOVERY_MAGIC = b"MONOJO_DRIVE_V1"
PROBE_MSG = b"MONOJO_DISCOVER"

# ---- util framing JSON sobre TCP ----
def recv_n(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def enviar_json(sock, obj):
    data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    hdr = struct.pack('!I', len(data))
    sock.sendall(hdr + data)

def recibir_json(sock):
    hdr = recv_n(sock, 4)
    if not hdr:
        return None
    (length,) = struct.unpack('!I', hdr)
    body = recv_n(sock, length)
    if not body:
        return None
    return json.loads(body.decode('utf-8'))

def nombre_seguro(name):
    return os.path.basename(name)

# ---- Discovery thread (responde probes y anuncia) ----
class DiscoveryThread(threading.Thread):
    def __init__(self, nombre, puerto_udp):
        super().__init__(daemon=True)
        self.nombre = nombre
        self.puerto_udp = puerto_udp
        self._running = threading.Event()
        # socket único para recv (bind) y send
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        try:
            self.sock.bind(('', self.puerto_udp))
        except Exception:
            # en algunas plataformas no se puede bindear; aun así intentaremos recibir respuestas
            pass
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass

    def run(self):
        self._running.set()
        last_announce = 0.0
        while self._running.is_set():
            # anunciar periódicamente
            now = time.time()
            if now - last_announce >= INTERVALO_ANNOUNCE:
                try:
                    payload = json.dumps({"nombre": self.nombre, "port": PUERTO_CONTROL}).encode('utf-8')
                    msg = DISCOVERY_MAGIC + b"|" + payload
                    # enviar broadcast amplio
                    try:
                        self.sock.sendto(msg, ('<broadcast>', self.puerto_udp))
                    except Exception:
                        pass
                    try:
                        self.sock.sendto(msg, ('255.255.255.255', self.puerto_udp))
                    except Exception:
                        pass
                except Exception:
                    pass
                last_announce = now
            # recibir probes (no bloquear mucho)
            try:
                self.sock.settimeout(0.25)
                data, addr = self.sock.recvfrom(4096)
                if not data:
                    continue
                if data.startswith(PROBE_MSG):
                    # responder unicast con info
                    try:
                        payload = json.dumps({"nombre": self.nombre, "port": PUERTO_CONTROL}).encode('utf-8')
                        msg = DISCOVERY_MAGIC + b"|" + payload
                        self.sock.sendto(msg, addr)
                    except Exception:
                        pass
            except socket.timeout:
                continue
            except Exception:
                # ignorar errores puntuales
                continue

    def stop(self):
        self._running.clear()
        try:
            self.sock.close()
        except Exception:
            pass

# ---- Servidor TCP principal ----
class MonojoServer(threading.Thread):
    def __init__(self, carpeta_drive, nombre, gui_queue):
        super().__init__(daemon=True)
        self.carpeta_drive = carpeta_drive
        os.makedirs(self.carpeta_drive, exist_ok=True)
        self.nombre = nombre
        self.gui_queue = gui_queue

        self.running = threading.Event()
        self.lock = threading.Lock()
        self.pending_requests = {}      # client_id -> {'name','ip','conn'}
        self.client_permissions = {}    # client_id -> 'rechazar'|'descarga'|'completo'
        self.next_client_id = 1
        self.tcp_sock = None
        self.discovery = None

    def run(self):
        self.running.set()
        # lanza discovery thread
        self.discovery = DiscoveryThread(self.nombre, PUERTO_DESCUBRIMIENTO)
        self.discovery.start()

        # servidor TCP
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind(('', PUERTO_CONTROL))
        self.tcp_sock.listen(8)

        while self.running.is_set():
            try:
                conn, addr = self.tcp_sock.accept()
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
            except Exception:
                pass

    def stop(self):
        self.running.clear()
        try:
            if self.discovery:
                self.discovery.stop()
        except Exception:
            pass
        try:
            if self.tcp_sock:
                self.tcp_sock.close()
        except Exception:
            pass

    def _handle_client(self, conn, addr):
        client_id = None
        try:
            mensaje = recibir_json(conn)
            if not mensaje or mensaje.get('type') != 'join':
                conn.close()
                return
            client_name = mensaje.get('name', 'desconocido')

            with self.lock:
                client_id = f"c{self.next_client_id}"
                self.next_client_id += 1
                self.pending_requests[client_id] = {'name': client_name, 'ip': addr[0], 'conn': conn}

            # notificar GUI para que admin decida
            self.gui_queue.put(('join_request', client_id, client_name, addr[0]))

            # esperar permiso (establecido desde GUI)
            permiso = None
            while self.running.is_set():
                with self.lock:
                    permiso = self.client_permissions.get(client_id)
                if permiso is not None:
                    break
                time.sleep(0.1)

            if permiso == 'rechazar' or permiso is None:
                try:
                    enviar_json(conn, {'type': 'join_response', 'status': 'rejected'})
                except Exception:
                    pass
                conn.close()
                with self.lock:
                    self.pending_requests.pop(client_id, None)
                self.gui_queue.put(('client_rejected', client_id))
                return

            # aceptar: enviar join_response indicando mode
            try:
                enviar_json(conn, {'type': 'join_response', 'status': 'ok', 'mode': permiso})
            except Exception:
                conn.close()
                return

            # manejar comandos (list/download/upload)
            while True:
                msg = recibir_json(conn)
                if msg is None:
                    break
                t = msg.get('type')
                if t == 'list':
                    archivos = []
                    for root, _, files in os.walk(self.carpeta_drive):
                        for f in files:
                            full = os.path.join(root, f)
                            rel = os.path.relpath(full, self.carpeta_drive)
                            archivos.append(rel)
                    enviar_json(conn, {'type': 'list_response', 'files': archivos})
                elif t == 'download':
                    fname = nombre_seguro(msg.get('filename', ''))
                    full = os.path.join(self.carpeta_drive, fname)
                    if not os.path.isfile(full):
                        enviar_json(conn, {'type': 'error', 'message': 'file not found'})
                        continue
                    size = os.path.getsize(full)
                    enviar_json(conn, {'type': 'download_ready', 'size': size})
                    with open(full, 'rb') as f:
                        while True:
                            chunk = f.read(TAM_BUFFER)
                            if not chunk:
                                break
                            conn.sendall(chunk)
                elif t == 'upload':
                    if permiso != 'completo':
                        enviar_json(conn, {'type': 'error', 'message': 'permission denied'})
                        continue
                    fname = nombre_seguro(msg.get('filename', ''))
                    size = int(msg.get('size', 0))
                    target = os.path.join(self.carpeta_drive, fname)
                    enviar_json(conn, {'type': 'upload_ready'})
                    received = 0
                    with open(target, 'wb') as f:
                        while received < size:
                            toread = min(TAM_BUFFER, size - received)
                            chunk = conn.recv(toread)
                            if not chunk:
                                break
                            f.write(chunk)
                            received += len(chunk)
                    if received == size:
                        enviar_json(conn, {'type': 'upload_done', 'filename': fname})
                        self.gui_queue.put(('file_uploaded', fname))
                    else:
                        enviar_json(conn, {'type': 'error', 'message': 'incomplete transfer'})
                else:
                    enviar_json(conn, {'type': 'error', 'message': 'unknown command'})
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
            with self.lock:
                if client_id:
                    self.pending_requests.pop(client_id, None)
                    self.client_permissions.pop(client_id, None)
            self.gui_queue.put(('client_disconnected', client_id))

    def set_permission(self, client_id, permiso):
        with self.lock:
            self.client_permissions[client_id] = permiso

# ---- GUI servidor (pide nombre/carpeta antes de iniciar) ----
class ServerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Monojo Drive - Servidor")
        if os.path.exists("monojo_verde.png"):
            try:
                img = tk.PhotoImage(file="monojo_verde.png")
                root.iconphoto(True, img)
            except Exception:
                pass

        self.gui_queue = queue.Queue()
        self.server = None

        # cargar settings si existen
        self.settings = {"nombre": "MonojoDrive", "carpeta": os.path.abspath("Drive")}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    s = json.load(f)
                    if isinstance(s, dict):
                        self.settings.update(s)
            except Exception:
                pass

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill='both', expand=True)

        ttk.Label(frm, text="Nombre del servidor:").grid(row=0, column=0, sticky='w')
        self.nombre_var = tk.StringVar(value=self.settings.get("nombre"))
        self.entry_nombre = ttk.Entry(frm, textvariable=self.nombre_var, width=40)
        self.entry_nombre.grid(row=0, column=1, sticky='w')

        ttk.Label(frm, text="Carpeta del Drive:").grid(row=1, column=0, sticky='w')
        self.carpeta_var = tk.StringVar(value=self.settings.get("carpeta"))
        self.entry_carpeta = ttk.Entry(frm, textvariable=self.carpeta_var, width=40)
        self.entry_carpeta.grid(row=1, column=1, sticky='w')
        ttk.Button(frm, text="Elegir carpeta", command=self._elegir_carpeta).grid(row=1, column=2, padx=6)

        self.btn_iniciar = ttk.Button(frm, text="Iniciar servidor", command=self._iniciar_servidor)
        self.btn_iniciar.grid(row=2, column=0, columnspan=3, pady=8)

        self.estado_lbl = ttk.Label(frm, text="Servidor detenido", foreground="red")
        self.estado_lbl.grid(row=2, column=2, padx=6)

        ttk.Label(frm, text="Solicitudes pendientes:").grid(row=3, column=0, sticky='w', pady=(6,0))
        self.pending_list = tk.Listbox(frm, width=80, height=8)
        self.pending_list.grid(row=4, column=0, columnspan=3, sticky='we')

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, pady=4)
        ttk.Button(btns, text="Rechazar", command=lambda: self._handle_action("rechazar")).pack(side='left', padx=4)
        ttk.Button(btns, text="Solo descargar", command=lambda: self._handle_action("descarga")).pack(side='left', padx=4)
        ttk.Button(btns, text="Subir y descargar", command=lambda: self._handle_action("completo")).pack(side='left', padx=4)

        ttk.Label(frm, text="Contenido del Drive:").grid(row=6, column=0, sticky='w', pady=(6,0))
        self.files_list = tk.Listbox(frm, width=100, height=12)
        self.files_list.grid(row=7, column=0, columnspan=3, sticky='we')

        # proceso de cola GUI
        self.root.after(200, self._process_queue)

    def _elegir_carpeta(self):
        sel = filedialog.askdirectory()
        if sel:
            self.carpeta_var.set(sel)

    def _save_settings(self):
        data = {"nombre": self.nombre_var.get(), "carpeta": self.carpeta_var.get()}
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _iniciar_servidor(self):
        if self.server:
            messagebox.showinfo("Servidor", "El servidor ya está en ejecución.")
            return
        carpeta = self.carpeta_var.get()
        nombre = self.nombre_var.get().strip() or "MonojoDrive"
        if not os.path.isdir(carpeta):
            try:
                os.makedirs(carpeta, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Carpeta", f"No se puede crear/usar la carpeta: {e}")
                return
        self._save_settings()
        self.server = MonojoServer(carpeta, nombre, self.gui_queue)
        self.server.start()
        # deshabilitar inputs de creación
        self.entry_nombre.configure(state='disabled')
        self.entry_carpeta.configure(state='disabled')
        self.btn_iniciar.configure(state='disabled')
        self.estado_lbl.configure(text="Servidor en ejecución", foreground="green")
        messagebox.showinfo("Servidor", "Servidor iniciado y anunciando en la LAN.")
        self._refresh_files()

    def _handle_action(self, permiso):
        sel = self.pending_list.curselection()
        if not sel:
            return
        idx = sel[0]
        txt = self.pending_list.get(idx)
        client_id = txt.split()[0]
        if not self.server:
            return
        self.server.set_permission(client_id, permiso)
        self.pending_list.delete(idx)

    def _refresh_files(self):
        self.files_list.delete(0, 'end')
        carpeta = self.carpeta_var.get()
        if not os.path.isdir(carpeta):
            return
        for root, _, files in os.walk(carpeta):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), carpeta)
                self.files_list.insert('end', rel)

    def _process_queue(self):
        while True:
            try:
                item = self.gui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(item)
        self.root.after(200, self._process_queue)

    def _handle_event(self, item):
        tipo = item[0]
        if tipo == 'join_request':
            _, client_id, client_name, ip = item
            display = f"{client_id} - {client_name} @ {ip}"
            self.pending_list.insert('end', display)
        elif tipo == 'file_uploaded':
            _, fname = item
            messagebox.showinfo("Archivo subido", f"Archivo subido: {fname}")
            self._refresh_files()
        elif tipo in ('client_rejected', 'client_disconnected'):
            cid = item[1] if len(item) > 1 else None
            if cid:
                self._remove_pending_by_id(cid)

    def _remove_pending_by_id(self, cid):
        for i in range(self.pending_list.size()):
            txt = self.pending_list.get(i)
            if txt.startswith(cid + ' ') or txt.startswith(cid + ' -'):
                self.pending_list.delete(i)
                break

if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("920x600")
    app = ServerGUI(root)
    root.mainloop()
