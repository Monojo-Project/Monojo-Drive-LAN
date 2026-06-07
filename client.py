#!/usr/bin/env python3

import os
import json
import socket
import struct
import threading
import time
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext, simpledialog

PUERTO_DESCUBRIMIENTO = 64000
PUERTO_CONTROL = 64001
TAM_BUFFER = 64 * 1024
DISCOVERY_MAGIC = b"MONOJO_DRIVE_V1"
PROBE_MSG = b"MONOJO_DISCOVER"

# --- Wrappers de Zenity con Fallback a Tkinter ---

def zenity_askstring(title, prompt, default=""):
    try:
        res = subprocess.run(['zenity', '--entry', f'--title={title}', f'--text={prompt}', f'--entry-text={default}'], capture_output=True, text=True)
        if res.returncode == 0: return res.stdout.strip('\n')
        if res.returncode == 1: return ""
    except FileNotFoundError: pass
    return simpledialog.askstring(title, prompt, initialvalue=default)

def zenity_asksaveas(default_name=""):
    try:
        res = subprocess.run(['zenity', '--file-selection', '--save', '--confirm-overwrite', f'--filename={default_name}'], capture_output=True, text=True)
        if res.returncode == 0: return res.stdout.strip('\n')
        if res.returncode == 1: return ""
    except FileNotFoundError: pass
    return filedialog.asksaveasfilename(initialfile=default_name)

def zenity_askopenfilename():
    try:
        res = subprocess.run(['zenity', '--file-selection'], capture_output=True, text=True)
        if res.returncode == 0: return res.stdout.strip('\n')
        if res.returncode == 1: return ""
    except FileNotFoundError: pass
    return filedialog.askopenfilename()

# -------------------------------------------------

def recv_n(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk: return None
        buf += chunk
    return buf

def enviar_json(sock, obj):
    try:
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        hdr = struct.pack('!I', len(data))
        sock.sendall(hdr + data)
        return True
    except Exception: return False

def recibir_json(sock):
    try:
        hdr = recv_n(sock, 4)
        if not hdr: return None
        length, = struct.unpack('!I', hdr)
        if length > 10 * 1024 * 1024: return None
        body = recv_n(sock, length)
        if not body: return None
        return json.loads(body.decode('utf-8'))
    except Exception: return None

def buscar_servidores(timeout=2.0):
    servidores = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception: pass
    s.settimeout(0.5)

    try:
        s.sendto(PROBE_MSG, ('<broadcast>', PUERTO_DESCUBRIMIENTO))
        s.sendto(PROBE_MSG, ('255.255.255.255', PUERTO_DESCUBRIMIENTO))
    except Exception: pass

    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            data, addr = s.recvfrom(4096)
            if not data or not data.startswith(DISCOVERY_MAGIC + b"|"): continue
            if data.startswith(DISCOVERY_MAGIC + b"|UPDATE|"): continue

            payload = data.split(b"|", 1)[1]
            info = json.loads(payload.decode('utf-8'))
            nombre = info.get('nombre', addr[0])
            port = int(info.get('port', PUERTO_CONTROL))
            servidores[nombre] = (addr[0], port)
        except socket.timeout: continue
        except Exception: break
    try: s.close()
    except Exception: pass
    return servidores

def format_size(size_bytes, unit_type):
    if unit_type == "Bytes": return f"{size_bytes} B"
    elif unit_type == "KB": return f"{size_bytes / 1024:.2f} KB"
    elif unit_type == "MB": return f"{size_bytes / (1024 ** 2):.2f} MB"
    elif unit_type == "GB": return f"{size_bytes / (1024 ** 3):.2f} GB"
    else: # Auto
        if size_bytes < 1024: return f"{size_bytes} B"
        elif size_bytes < 1024 ** 2: return f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 ** 3: return f"{size_bytes / (1024 ** 2):.2f} MB"
        else: return f"{size_bytes / (1024 ** 3):.2f} GB"

class ClienteGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Monojo Drive LAN - Cliente")
        self.root.geometry("1100x700")

        self.servidores = {}
        self.sock = None
        self.mode = None
        self.transferring = False
        self.nombre_cliente = None
        self.servidor_conectado = None

        self.current_path = ""
        self.current_items = []
        self.displayed_items = [] # Mapeo 1:1 con la Listbox
        self.show_hidden = False

        self.root.bind('<Control-h>', self._toggle_hidden)
        self.root.bind('<Control-H>', self._toggle_hidden)

        self.buscar_thread = None
        self.monitor_thread = None
        self.udp_listener_thread = None

        self.main_frame = ttk.Frame(root)
        self.main_frame.pack(fill='both', expand=True)

        self._crear_ui_login()

    def _crear_ui_login(self):
        for widget in self.main_frame.winfo_children(): widget.destroy()

        login_frame = ttk.Frame(self.main_frame, padding=20)
        login_frame.pack(fill='both', expand=True)

        title_frame = ttk.Frame(login_frame)
        title_frame.pack(fill='x', pady=20)
        ttk.Label(title_frame, text="📄 Monojo Drive LAN 📁", font=('Arial', 16, 'bold')).pack()
        ttk.Label(title_frame, text="Conéctate a un servidor", font=('Arial', 10)).pack()

        name_frame = ttk.LabelFrame(login_frame, text="Tu nombre", padding=10)
        name_frame.pack(fill='x', pady=10)
        self.nombre_var = tk.StringVar(value="Cliente")
        ttk.Entry(name_frame, textvariable=self.nombre_var, font=('Arial', 12), width=30).pack(fill='x')

        servers_frame = ttk.LabelFrame(login_frame, text="Servidores disponibles", padding=10)
        servers_frame.pack(fill='both', expand=True, pady=10)

        btn_frame = ttk.Frame(servers_frame)
        btn_frame.pack(fill='x', pady=(0, 10))
        ttk.Button(btn_frame, text="🔄 Buscar servidores", command=self.refrescar).pack(side='left', padx=5)

        self.serv_tree = ttk.Treeview(servers_frame, columns=('ip', 'puerto', 'nombre'), show='headings', height=8)
        self.serv_tree.heading('nombre', text='Nombre del servidor')
        self.serv_tree.heading('ip', text='IP')
        self.serv_tree.heading('puerto', text='Puerto')
        self.serv_tree.column('nombre', width=250)
        self.serv_tree.column('ip', width=150)
        self.serv_tree.column('puerto', width=80)
        self.serv_tree.pack(fill='both', expand=True)

        self.serv_tree.bind('<Double-1>', lambda e: self.conectar())

        ttk.Button(login_frame, text="🔗 Conectar", command=self.conectar, width=30).pack(pady=20)
        self.buscar_automatico()

    def _crear_ui_principal(self):
        for widget in self.main_frame.winfo_children(): widget.destroy()

        header_frame = ttk.Frame(self.main_frame, padding=8)
        header_frame.pack(fill='x', padx=5, pady=5)

        ttk.Label(header_frame, text=f"✓ Conectado a: {self.servidor_conectado}", font=('Arial', 11, 'bold'), foreground='green').pack(side='left')
        ttk.Label(header_frame, text=f"Como: {self.nombre_cliente}", font=('Arial', 10)).pack(side='left', padx=20)
        ttk.Label(header_frame, text=f"Permisos: {self.mode}", font=('Arial', 10)).pack(side='left', padx=20)
        ttk.Button(header_frame, text="🔌 Desconectar", command=self.desconectar).pack(side='right', padx=5)

        self.notebook = ttk.Notebook(self.main_frame)
        self.notebook.pack(fill='both', expand=True, padx=5, pady=5)

        frm_archivos = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frm_archivos, text="📁 Archivos")

        nav_frame = ttk.Frame(frm_archivos)
        nav_frame.pack(fill='x', pady=5)
        ttk.Button(nav_frame, text="⬅️ Atrás", command=self._ir_atras).pack(side='left', padx=5)
        self.lbl_ruta = ttk.Label(nav_frame, text="Ruta: /", font=('Arial', 10, 'bold'))
        self.lbl_ruta.pack(side='left', padx=10)
        self.lbl_ocultos = ttk.Label(nav_frame, text="(Ocultos: No - CTRL+H)", font=('Arial', 9, 'italic'), foreground='gray')
        self.lbl_ocultos.pack(side='left', padx=10)

        # Opciones de vista a la derecha
        self.unit_var = tk.StringVar(value="Auto")
        cmb_units = ttk.Combobox(nav_frame, textvariable=self.unit_var, values=["Auto", "Bytes", "KB", "MB", "GB"], state="readonly", width=8)
        cmb_units.pack(side='right', padx=5)
        cmb_units.bind("<<ComboboxSelected>>", lambda e: self._listar_archivos())
        ttk.Label(nav_frame, text="Medida:").pack(side='right')

        self.btn_nuevo_archivo = ttk.Button(nav_frame, text="📄 Nuevo archivo", command=self._crear_archivo_vacio, state='disabled')
        self.btn_nuevo_archivo.pack(side='right', padx=5)

        self.btn_nueva_carpeta = ttk.Button(nav_frame, text="📁 Nueva carpeta", command=self._crear_carpeta, state='disabled')
        self.btn_nueva_carpeta.pack(side='right', padx=5)

        scroll = ttk.Scrollbar(frm_archivos)
        scroll.pack(side='right', fill='y')
        self.remote_list = tk.Listbox(frm_archivos, height=15, yscrollcommand=scroll.set, font=('Arial', 11))
        self.remote_list.pack(fill='both', expand=True, pady=5)
        scroll.config(command=self.remote_list.yview)
        self.remote_list.bind('<Double-1>', self._on_doble_click)

        btn_archivos = ttk.Frame(frm_archivos)
        btn_archivos.pack(fill='x', pady=5)
        ttk.Button(btn_archivos, text="⬇️ Descargar", command=self.descargar).pack(side='left', padx=5)

        self.btn_subir = ttk.Button(btn_archivos, text="⬆️ Subir archivo", command=self.subir, state='disabled')
        self.btn_subir.pack(side='left', padx=5)

        self.btn_renombrar = ttk.Button(btn_archivos, text="✏️ Renombrar/Mover", command=self.renombrar, state='disabled')
        self.btn_renombrar.pack(side='left', padx=5)

        self.btn_editar = ttk.Button(btn_archivos, text="📝 Editar Texto", command=self.editar_texto, state='disabled')
        self.btn_editar.pack(side='left', padx=5)

        ttk.Button(btn_archivos, text="🔄 Refrescar", command=self._listar_archivos).pack(side='left', padx=5)

        frm_log = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frm_log, text="📋 Log")
        self.log_text = scrolledtext.ScrolledText(frm_log, height=20, state='disabled', wrap='word', font=('Courier', 9))
        self.log_text.pack(fill='both', expand=True)

        self._log("✓ Conectado al servidor")

        if self.mode == 'completo':
            self.btn_subir.config(state='normal')
            self.btn_nueva_carpeta.config(state='normal')
            self.btn_nuevo_archivo.config(state='normal')
            self.btn_renombrar.config(state='normal')
            self.btn_editar.config(state='normal')

        self.notebook.select(0)
        self.current_path = ""
        self._listar_archivos()
        self._iniciar_escucha_udp()

    def _toggle_hidden(self, event=None):
        self.show_hidden = not self.show_hidden
        if hasattr(self, 'lbl_ocultos'):
            self.lbl_ocultos.config(text=f"(Ocultos: {'Sí' if self.show_hidden else 'No'} - CTRL+H)")
        self._listar_archivos()

    def _log(self, msg):
        if hasattr(self, 'log_text'):
            self.log_text.config(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
            self.root.update_idletasks()

    def buscar_automatico(self):
        def buscar():
            while self.root.winfo_exists() and not self.sock:
                try:
                    nuevos = buscar_servidores(timeout=2.0)
                    if nuevos != self.servidores:
                        self.servidores = nuevos
                        if hasattr(self, 'serv_tree'): self.root.after(0, self._actualizar_lista_servidores)
                except Exception: pass
                for _ in range(50):
                    if not self.root.winfo_exists() or self.sock: return
                    time.sleep(0.1)
        if self.buscar_thread is None or not self.buscar_thread.is_alive():
            self.buscar_thread = threading.Thread(target=buscar, daemon=True)
            self.buscar_thread.start()

    def _iniciar_escucha_udp(self):
        def escuchar():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('', PUERTO_DESCUBRIMIENTO))
            except Exception: return
            s.settimeout(1.0)
            while self.root.winfo_exists() and self.sock:
                try:
                    data, addr = s.recvfrom(4096)
                    if data.startswith(DISCOVERY_MAGIC + b"|UPDATE|"):
                        info = json.loads(data.split(b"|", 2)[2].decode('utf-8'))
                        if info.get('nombre') == self.servidor_conectado and not self.transferring:
                            self.root.after(0, self._listar_archivos)
                except socket.timeout: continue
                except Exception: break
            try: s.close()
            except Exception: pass

        if self.udp_listener_thread is None or not self.udp_listener_thread.is_alive():
            self.udp_listener_thread = threading.Thread(target=escuchar, daemon=True)
            self.udp_listener_thread.start()

    def refrescar(self):
        try:
            self.servidores = buscar_servidores(timeout=2.0)
            self._actualizar_lista_servidores()
        except Exception as e: messagebox.showerror("Error", f"Error al buscar: {e}")

    def _actualizar_lista_servidores(self):
        if not hasattr(self, 'serv_tree'): return
        for i in self.serv_tree.get_children(): self.serv_tree.delete(i)
        for nombre, (ip, port) in self.servidores.items():
            self.serv_tree.insert('', 'end', values=(ip, port, nombre))

    def conectar(self):
        nombre = self.nombre_var.get().strip()
        if not nombre or len(nombre) < 2: return messagebox.showwarning("Error", "Nombre inválido")
        sel = self.serv_tree.selection()
        if not sel: return messagebox.showwarning("Conectar", "Selecciona un servidor.")

        vals = self.serv_tree.item(sel[0], 'values')
        ip, puerto, servidor_nombre = vals[0], int(vals[1]), vals[2]

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(120)
            sock.connect((ip, puerto))

            if not enviar_json(sock, {'type': 'join', 'name': nombre}): raise Exception("Fallo en join")
            resp = recibir_json(sock)
            if not resp or resp.get('status') != 'ok': raise Exception("Rechazado por el servidor")

            self.sock = sock
            self.sock.settimeout(None)
            self.mode = resp.get('mode', 'desconocido')
            self.nombre_cliente, self.servidor_conectado = nombre, servidor_nombre

            self.monitor_thread = threading.Thread(target=self._monitorear_conexion, daemon=True)
            self.monitor_thread.start()
            self._crear_ui_principal()
        except Exception as e:
            messagebox.showerror("Conexión", f"Error: {e}")
            if self.sock:
                try: self.sock.close()
                except Exception: pass
                self.sock = None

    def _monitorear_conexion(self):
        while self.sock and self.root.winfo_exists():
            if not self.transferring:
                try:
                    self.sock.setblocking(False)
                    data = self.sock.recv(1, socket.MSG_PEEK)
                    if data == b'':
                        self.sock.setblocking(True)
                        self.root.after(0, self._notificar_cierre_servidor)
                        break
                except BlockingIOError: pass
                except Exception:
                    self.root.after(0, self._notificar_cierre_servidor)
                    break
                finally:
                    try: self.sock.setblocking(True)
                    except Exception: pass
            time.sleep(0.5)

    def _notificar_cierre_servidor(self):
        try: messagebox.showwarning("Servidor desconectado", "El servidor se ha cerrado.")
        except Exception: pass
        self.desconectar()

    def desconectar(self):
        if self.sock:
            try: self.sock.close()
            except Exception: pass
            self.sock = None
        self.mode = None
        self._crear_ui_login()

    def _ir_atras(self):
        if self.current_path:
            self.current_path = os.path.dirname(self.current_path)
            self._listar_archivos()

    def _on_doble_click(self, event):
        sel = self.remote_list.curselection()
        if not sel: return
        item = self.displayed_items[sel[0]]
        if item['is_dir']:
            self.current_path = os.path.join(self.current_path, item['name']).replace('\\', '/')
            self._listar_archivos()

    def _crear_carpeta(self):
        if self.mode != 'completo': return
        nombre = zenity_askstring("Nueva Carpeta", "Nombre de la carpeta:")
        if not nombre: return
        ruta_nueva = os.path.join(self.current_path, nombre).replace('\\', '/')
        try:
            self.transferring = True
            enviar_json(self.sock, {'type': 'create_folder', 'path': ruta_nueva})
            resp = recibir_json(self.sock)
            if resp and resp.get('type') == 'folder_created':
                self.transferring = False
                self._listar_archivos()
            else: messagebox.showerror("Error", resp.get('message', 'No se pudo crear'))
        except Exception as e: messagebox.showerror("Error", str(e))
        finally: self.transferring = False

    def _crear_archivo_vacio(self):
        if self.mode != 'completo': return
        nombre = zenity_askstring("Nuevo Archivo", "Nombre del archivo (ej: texto.txt):")
        if not nombre: return
        ruta_nueva = os.path.join(self.current_path, nombre).replace('\\', '/')
        try:
            self.transferring = True
            enviar_json(self.sock, {'type': 'create_file', 'path': ruta_nueva})
            resp = recibir_json(self.sock)
            if resp and resp.get('type') == 'file_created':
                self.transferring = False
                self._listar_archivos()
            else:
                messagebox.showerror("Error", resp.get('message', 'No se pudo crear el archivo'))
        except Exception as e: messagebox.showerror("Error", str(e))
        finally: self.transferring = False

    def renombrar(self):
        if self.mode != 'completo': return
        sel = self.remote_list.curselection()
        if not sel: return
        item = self.displayed_items[sel[0]]

        viejo_path = os.path.join(self.current_path, item['name']).replace('\\', '/')
        nuevo_nombre = zenity_askstring("Renombrar / Mover", "Nuevo nombre o ruta relativa:", default=item['name'])

        if not nuevo_nombre or nuevo_nombre == item['name']: return
        nuevo_path = os.path.join(self.current_path, nuevo_nombre).replace('\\', '/')

        try:
            self.transferring = True
            enviar_json(self.sock, {'type': 'rename', 'old': viejo_path, 'new': nuevo_path})
            resp = recibir_json(self.sock)
            if resp and resp.get('type') == 'rename_done':
                self.transferring = False
                self._listar_archivos()
            else:
                messagebox.showerror("Error", resp.get('message', 'Fallo al renombrar'))
        except Exception as e:
            messagebox.showerror("Error", str(e))
        finally:
            self.transferring = False

    def editar_texto(self):
        if self.mode != 'completo': return
        sel = self.remote_list.curselection()
        if not sel: return
        item = self.displayed_items[sel[0]]

        if item['is_dir']: return messagebox.showinfo("Info", "Selecciona un archivo, no una carpeta.")

        filepath = os.path.join(self.current_path, item['name']).replace('\\', '/')
        try:
            self.transferring = True
            enviar_json(self.sock, {'type': 'read_text', 'filepath': filepath})
            resp = recibir_json(self.sock)
            if not resp or resp.get('type') != 'text_content':
                raise Exception(resp.get('message', "No se pudo leer el archivo."))

            contenido = resp.get('content', '')
            self._abrir_editor(filepath, contenido)
        except Exception as e:
            messagebox.showerror("Error", str(e))
        finally:
            self.transferring = False

    def _abrir_editor(self, filepath, contenido):
        top = tk.Toplevel(self.root)
        top.title(f"Editor Texto - {os.path.basename(filepath)}")
        top.geometry("700x500")

        txt = scrolledtext.ScrolledText(top, wrap='word', font=('Consolas', 11))
        txt.pack(fill='both', expand=True, padx=5, pady=5)
        txt.insert('1.0', contenido)

        def guardar():
            nuevo_contenido = txt.get('1.0', 'end-1c')
            try:
                self.transferring = True
                enviar_json(self.sock, {'type': 'save_text', 'filepath': filepath, 'content': nuevo_contenido})
                resp = recibir_json(self.sock)
                if resp and resp.get('type') == 'save_done':
                    messagebox.showinfo("Guardado", "Archivo guardado exitosamente.", parent=top)
                    top.destroy()
                else:
                    messagebox.showerror("Error", resp.get('message', 'Fallo al guardar'), parent=top)
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=top)
            finally:
                self.transferring = False
                self._listar_archivos()

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill='x', pady=5)
        ttk.Button(btn_frame, text="💾 Guardar Cambios", command=guardar).pack(side='right', padx=5)
        ttk.Button(btn_frame, text="Cancelar", command=top.destroy).pack(side='right', padx=5)

    def _listar_archivos(self):
        if not self.sock or self.transferring: return
        self.lbl_ruta.config(text=f"Ruta: /{self.current_path}")
        try:
            self.transferring = True
            enviar_json(self.sock, {'type': 'list', 'path': self.current_path})
            resp = recibir_json(self.sock)
            if not resp or resp.get('type') != 'list_response': raise Exception("Respuesta inválida")

            self.remote_list.delete(0, 'end')
            self.current_items = resp.get('items', [])
            self.displayed_items = []

            img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg'}

            for item in self.current_items:
                if not self.show_hidden and item['name'].startswith('.'): continue

                self.displayed_items.append(item)

                if item['is_dir']:
                    self.remote_list.insert('end', f"📁 {item['name']}")
                else:
                    ext = os.path.splitext(item['name'])[1].lower()
                    emoji = "🖼️" if ext in img_exts else "📄"
                    tamano_formateado = format_size(item['size'], self.unit_var.get())
                    self.remote_list.insert('end', f"{emoji} {item['name']} ({tamano_formateado})")

        except Exception:
            self.current_path = ""
            self.lbl_ruta.config(text="Ruta: /")
        finally: self.transferring = False

    def descargar(self):
        sel = self.remote_list.curselection()
        if not sel: return
        item = self.displayed_items[sel[0]]

        filepath = os.path.join(self.current_path, item['name']).replace('\\', '/')
        if self.transferring: return

        try:
            self.transferring = True
            if item['is_dir']:
                ruta = zenity_asksaveas(default_name=item['name'] + ".zip")
                if not ruta: return
                self._log(f"Comprimiendo y descargando carpeta: {item['name']}...")
                enviar_json(self.sock, {'type': 'download_folder', 'path': filepath})
            else:
                ruta = zenity_asksaveas(default_name=item['name'])
                if not ruta: return
                self._log(f"Descargando archivo: {item['name']}...")
                enviar_json(self.sock, {'type': 'download', 'filepath': filepath})

            resp = recibir_json(self.sock)
            if not resp or resp.get('type') != 'download_ready': raise Exception("Petición Rechazada")

            size = int(resp.get('size', 0))
            recibidos = 0
            with open(ruta, 'wb') as f:
                while recibidos < size:
                    chunk = self.sock.recv(min(TAM_BUFFER, size - recibidos))
                    if not chunk: break
                    f.write(chunk)
                    recibidos += len(chunk)

            if recibidos == size: messagebox.showinfo("Descarga", "Descarga completada.")
            else: raise Exception("Transferencia incompleta")
        except Exception as e: messagebox.showerror("Error", str(e))
        finally: self.transferring = False

    def subir(self):
        if self.mode != 'completo' or self.transferring: return
        ruta = zenity_askopenfilename()
        if not ruta: return
        nombre = os.path.basename(ruta)
        size = os.path.getsize(ruta)

        try:
            self.transferring = True
            enviar_json(self.sock, {'type': 'upload', 'filename': nombre, 'path': self.current_path, 'size': size})
            if recibir_json(self.sock).get('type') != 'upload_ready': raise Exception("Rechazada")

            with open(ruta, 'rb') as f:
                while True:
                    chunk = f.read(TAM_BUFFER)
                    if not chunk: break
                    self.sock.sendall(chunk)

            if recibir_json(self.sock).get('type') == 'upload_done': self._listar_archivos()
            else: raise Exception("Error de servidor")
        except Exception as e: messagebox.showerror("Error", str(e))
        finally: self.transferring = False

if __name__ == "__main__":
    root = tk.Tk(className="monojo_drive_lan_client")
    app = ClienteGUI(root)
    root.mainloop()
