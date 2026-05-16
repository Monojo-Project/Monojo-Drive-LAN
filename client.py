#!/usr/bin/env python3

import os
import json
import socket
import struct
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

PUERTO_DESCUBRIMIENTO = 64000
PUERTO_CONTROL = 64001
TAM_BUFFER = 64 * 1024
DISCOVERY_MAGIC = b"MONOJO_DRIVE_V1"
PROBE_MSG = b"MONOJO_DISCOVER"

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

def buscar_servidores(timeout=1.0):
    """
    Envía probe broadcast y escucha respuestas unicast en el mismo socket.
    Devuelve dict: nombre -> (ip, port)
    """
    servidores = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    s.settimeout(timeout)
    # enviar probe a broadcast amplio
    try:
        s.sendto(PROBE_MSG, ('<broadcast>', PUERTO_DESCUBRIMIENTO))
    except Exception:
        pass
    try:
        s.sendto(PROBE_MSG, ('255.255.255.255', PUERTO_DESCUBRIMIENTO))
    except Exception:
        pass
    inicio = time.time()
    try:
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            if not data:
                continue
            if data.startswith(DISCOVERY_MAGIC + b"|"):
                try:
                    payload = data.split(b"|", 1)[1]
                    info = json.loads(payload.decode('utf-8'))
                    nombre = info.get('nombre') or addr[0]
                    port = int(info.get('port', PUERTO_CONTROL))
                    servidores[nombre] = (addr[0], port)
                except Exception:
                    pass
            if time.time() - inicio > timeout:
                break
    finally:
        try:
            s.close()
        except Exception:
            pass
    return servidores

class ClienteGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Monojo Drive - Cliente")
        if os.path.exists("monojo_verde.png"):
            try:
                img = tk.PhotoImage(file="monojo_verde.png")
                root.iconphoto(True, img)
            except Exception:
                pass

        frm = ttk.Frame(root, padding=8)
        frm.pack(fill='both', expand=True)

        ttk.Button(frm, text="Refrescar servidores", command=self.refrescar).grid(row=0, column=0, sticky='w')

        self.serv_tree = ttk.Treeview(frm, columns=('ip','port','nombre'), show='headings', height=6)
        self.serv_tree.heading('ip', text='IP')
        self.serv_tree.heading('port', text='Puerto')
        self.serv_tree.heading('nombre', text='Nombre')
        self.serv_tree.grid(row=1, column=0, columnspan=3, sticky='we', pady=6)

        ttk.Button(frm, text="Conectar", command=self.conectar).grid(row=2, column=0, sticky='w')

        ttk.Label(frm, text="Archivos remotos:").grid(row=3, column=0, sticky='w', pady=(8,0))
        self.remote_list = tk.Listbox(frm, width=80, height=12)
        self.remote_list.grid(row=4, column=0, columnspan=3, sticky='we')

        self.btn_descargar = ttk.Button(frm, text="Descargar seleccionado", command=self.descargar)
        self.btn_descargar.grid(row=5, column=0, pady=6, sticky='w')

        self.btn_subir = ttk.Button(frm, text="Subir archivo (si tienes permiso)", command=self.subir)
        self.btn_subir.grid(row=5, column=1, pady=6, sticky='w')
        self.btn_subir.state(['disabled'])

        self.servidores = {}
        self.sock = None
        self.mode = None

    def refrescar(self):
        for i in self.serv_tree.get_children():
            self.serv_tree.delete(i)
        self.servidores = buscar_servidores(timeout=1.0)
        for nombre, (ip, port) in self.servidores.items():
            self.serv_tree.insert('', 'end', values=(ip, port, nombre))

    def conectar(self):
        sel = self.serv_tree.selection()
        if not sel:
            messagebox.showwarning("Conectar", "Selecciona un servidor.")
            return
        vals = self.serv_tree.item(sel[0], 'values')
        ip, port = vals[0], int(vals[1])
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(6)
            sock.connect((ip, port))
            enviar_json(sock, {'type': 'join', 'name': 'ClienteMonojo'})
            resp = recibir_json(sock)
            if not resp:
                messagebox.showerror("Error", "Sin respuesta del servidor.")
                sock.close()
                return
            if resp.get('status') != 'ok':
                messagebox.showerror("Conexión", "Servidor rechazó la unión.")
                sock.close()
                return
            self.sock = sock
            self.mode = resp.get('mode')
            messagebox.showinfo("Conectado", f"Conectado. Modo: {self.mode}")
            # habilitar/deshabilitar subir
            if self.mode == 'completo':
                try:
                    self.btn_subir.state(['!disabled'])
                except Exception:
                    pass
            else:
                try:
                    self.btn_subir.state(['disabled'])
                except Exception:
                    pass
            self._listar_archivos()
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo conectar: {e}")

    def _listar_archivos(self):
        if not self.sock:
            return
        try:
            enviar_json(self.sock, {'type': 'list'})
            resp = recibir_json(self.sock)
            if resp and resp.get('type') == 'list_response':
                self.remote_list.delete(0, 'end')
                for f in resp.get('files', []):
                    self.remote_list.insert('end', f)
        except Exception as e:
            messagebox.showerror("Error", f"Error al listar archivos: {e}")

    def descargar(self):
        if not self.sock:
            messagebox.showwarning("Descargar", "No estás conectado.")
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        fname = self.remote_list.get(sel[0])
        try:
            enviar_json(self.sock, {'type': 'download', 'filename': fname})
            resp = recibir_json(self.sock)
            if not resp or resp.get('type') != 'download_ready':
                messagebox.showerror("Error", "No se pudo iniciar la descarga.")
                return
            size = int(resp.get('size', 0))
            ruta = filedialog.asksaveasfilename(initialfile=os.path.basename(fname))
            if not ruta:
                restante = size
                while restante > 0:
                    chunk = self.sock.recv(min(TAM_BUFFER, restante))
                    if not chunk:
                        break
                    restante -= len(chunk)
                return
            recibidos = 0
            with open(ruta, 'wb') as f:
                while recibidos < size:
                    chunk = self.sock.recv(min(TAM_BUFFER, size - recibidos))
                    if not chunk:
                        break
                    f.write(chunk)
                    recibidos += len(chunk)
            if recibidos == size:
                messagebox.showinfo("Descarga", f"Descargado: {ruta}")
            else:
                messagebox.showerror("Descarga", "Transferencia incompleta.")
        except Exception as e:
            messagebox.showerror("Error", f"Error en descarga: {e}")

    def subir(self):
        if not self.sock:
            messagebox.showwarning("Subir", "No estás conectado.")
            return
        if self.mode != 'completo':
            messagebox.showwarning("Subir", "No tienes permiso para subir.")
            return
        ruta = filedialog.askopenfilename()
        if not ruta:
            return
        nombre = os.path.basename(ruta)
        size = os.path.getsize(ruta)
        try:
            enviar_json(self.sock, {'type': 'upload', 'filename': nombre, 'size': size})
            resp = recibir_json(self.sock)
            if not resp or resp.get('type') != 'upload_ready':
                messagebox.showerror("Subir", "El servidor rechazó la subida.")
                return
            with open(ruta, 'rb') as f:
                while True:
                    chunk = f.read(TAM_BUFFER)
                    if not chunk:
                        break
                    self.sock.sendall(chunk)
            final = recibir_json(self.sock)
            if final and final.get('type') == 'upload_done':
                messagebox.showinfo("Subir", f"Fichero subido: {final.get('filename')}")
                self._listar_archivos()
            else:
                messagebox.showerror("Subir", "Error en la subida.")
        except Exception as e:
            messagebox.showerror("Error", f"Error al subir: {e}")

if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("860x600")
    app = ClienteGUI(root)
    root.mainloop()
