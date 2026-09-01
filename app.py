import tkinter as tk
from tkinter import ttk
import uuid, datetime, qrcode, requests, os, time, threading, json, socket, subprocess
from PIL import Image, ImageTk
from smbus2 import SMBus
from escpos.printer import Serial as SerialPrinter

# Import your custom database module
import db_module

# --- Hardware & File Config ---
I2C_BUS = 1
NANO_ADDR = 0x08
PRINTER_PORT = '/dev/serial0' 
BAUD_RATE = 9600
AGENT_FILE = "agent_config.txt" 
LOG_FILE = "transaction_logs.json" 
HOLD_DELAY = 2 
IDLE_SLEEP_MS = 2 * 60 * 1000  # 2 minutes of inactivity before the display sleeps

try:
    printer = SerialPrinter(devfile=PRINTER_PORT, baudrate=BAUD_RATE, profile="POS-5890")
except:
    printer = None

class ERPOSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.scr_w = self.winfo_screenwidth()
        self.scr_h = self.winfo_screenheight()
        self.geometry(f"{self.scr_w}x{self.scr_h}+0+0")
        self.attributes('-fullscreen', True)
        self.overrideredirect(True)
        self.configure(bg="#F4F7F6")

        # Initialize Local SQLite Schema via your module
        try:
            db_module.create_table()
        except Exception as e:
            print(f"Local SQLite Database Initialization Error: {e}")

        try:
            self.icon_history = ImageTk.PhotoImage(Image.open("HISTORY.png").resize((50, 50)))
            self.icon_settings = ImageTk.PhotoImage(Image.open("settings.png").resize((50, 50)))
            bg_raw = Image.open("recycling_bg.png")
            self.bg_main = ImageTk.PhotoImage(bg_raw.resize((self.scr_w, self.scr_h), Image.Resampling.LANCZOS))
        except Exception as e:
            print(f"Asset Load Error: {e}")
            self.icon_history = self.icon_settings = self.bg_main = None

        self.raw_weight = 0.0
        self.daemon_running = True
        self.ui_loop_active = False 
        self.active_after_id = None 
        self.basket = []
        self.tare_val = 0.0
        self.is_held = False
        self.is_manual_mode = False  
        self.last_stable_weight = 0.0
        self.stable_start_time = time.time()
        self.last_weight = 0.0
        self.countdown = HOLD_DELAY
        self.selected_hub = ""

        self.hubs_list = [
            "Sahara Ogba Hub 2", "Sahara Ojodu Hub 1", "Sahara Ibeju Lekki", "Sahara Ijede Hub",
            "Sahara Lekki II Hub", "UniAbuja 2 RVM", "UniAbuja 1 RVM", "NNPC Garki II RVM",
            "NNPC Lifecamp RVM", "NNPC Lugbe RVM", "NNPC Gaduwa RVM", "Head of Service Abuja",
            "Gbazango RVM Hub", "Central Metro Station", "National Assembly", "UN House Abuja",
            "Shoprite Wuse", "Lagos Main Yard", "Truck Central", "Min. of Env. Mabushi",
            "Dang Outlets", "NNPC Yaba RVM", "Green Building RVM", "AEPB HQ RVM",
            "Central Park RVM", "Apo Main Yard", "Kuje Recycle Center"
        ]

        self.materials_map = {
            "PET": "Plastic bottles -PET", "BCP": "Carton - BCP", "HDPE": "Hard Plastic - HDPE",
            "PAP": "Paper - PAP", "PWS": "Pure Water Sachet - PWS", "MET": "Ewaste MET",
            "SHW": "Shrink - SHW", "SPB": "Single Use Plastic Bags - SPB", "MIX": "Mixed - MIX",
            "CAN": "Aluminum - ALU", "Ewaste": "Ewaste PLA", "BAT": "Ewaste BAT", "GLA": "Glass"
        }
        self.materials_list = [{"display": v, "system": k} for k, v in self.materials_map.items()]
        
        threading.Thread(target=self.hardware_daemon, daemon=True).start()

        # --- Idle sleep setup ---
        self.is_sleeping = False
        self.idle_after_id = None
        self.sleep_overlay = None
        self.display_sleep_supported = True  # flips to False if wlopm calls fail, so we stop retrying
        # bind_all only lets us OBSERVE activity, it can't block a Button's
        # own command from firing underneath it -- "break" doesn't cross
        # from a bind_all handler into a widget's command= callback. So we
        # use bind_all purely to reset the idle timer while awake...
        self.bind_all("<Button-1>", self._on_activity_while_awake, add="+")
        self.bind_all("<Motion>", self._on_activity_while_awake, add="+")
        self._reset_idle_timer()

        self.agent_id = self.load_agent_id() 
        if self.agent_id:
            self.show_customer_login()
        else:
            self.show_agent_auth()

    def _detect_wayland_display(self):
        """Finds the real Wayland socket name by checking what's actually on
        disk, instead of guessing a name like 'wayland-0' or 'wayland-1' --
        that guess breaks across reboots/Pi's/OS versions, this doesn't.
        Result is cached on self after the first successful detection."""
        if getattr(self, "_wayland_display_cache", None):
            return self._wayland_display_cache

        # If the environment already has it (e.g. running from the desktop
        # session directly), trust that first -- it's the ground truth.
        env_val = os.environ.get("WAYLAND_DISPLAY")
        if env_val:
            self._wayland_display_cache = env_val
            return env_val

        # Otherwise scan /run/user/<uid>/ for the actual wayland-* socket file.
        run_dir = f"/run/user/{os.getuid()}"
        try:
            candidates = sorted(
                f for f in os.listdir(run_dir)
                if f.startswith("wayland-") and not f.endswith(".lock")
            )
        except (FileNotFoundError, PermissionError) as e:
            print(f"Could not scan {run_dir} for a Wayland socket: {e}")
            candidates = []

        if candidates:
            self._wayland_display_cache = candidates[0]
            return candidates[0]

        return None  # genuinely couldn't find one -- caller should fail loudly, not guess

    def _run_wlopm(self, *args):
        """Runs a wlopm command against the Wayland/labwc display, e.g. _run_wlopm('--off', '*')."""
        if not self.display_sleep_supported:
            return False

        wayland_display = self._detect_wayland_display()
        if not wayland_display:
            print("Could not detect a Wayland display socket -- screen sleep disabled. "
                  "Check that a desktop session is actually running, and that "
                  f"/run/user/{os.getuid()}/ contains a wayland-* socket.")
            self.display_sleep_supported = False
            return False

        try:
            env = dict(os.environ)
            env["WAYLAND_DISPLAY"] = wayland_display
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            subprocess.run(["wlopm"] + list(args), env=env, check=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3)
            return True
        except FileNotFoundError:
            print("wlopm not found -- screen sleep disabled (sudo apt install wlopm).")
            self.display_sleep_supported = False
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
            print(f"wlopm {' '.join(args)} failed: {stderr.strip()}")
            # The cached socket name might be stale (e.g. compositor restarted
            # with a new socket) -- clear it so the next attempt re-detects
            # instead of repeating the same failure forever.
            self._wayland_display_cache = None
        except subprocess.TimeoutExpired:
            print(f"wlopm {' '.join(args)} timed out.")
        return False

    def _reset_idle_timer(self):
        if self.idle_after_id:
            self.after_cancel(self.idle_after_id)
        self.idle_after_id = self.after(IDLE_SLEEP_MS, self._go_to_sleep)

    def _on_activity_while_awake(self, event=None):
        # Only resets the timer. Never fires while asleep, because the
        # overlay (see _go_to_sleep) physically sits on top of and
        # intercepts every touch before it can reach this binding or any
        # button underneath -- that's what actually stops click-through.
        self._reset_idle_timer()

    def _go_to_sleep(self):
        self.is_sleeping = True
        self._run_wlopm("--off", "*")

        # Full-screen, top-most catcher: because this Frame is the actual
        # widget under the cursor, IT receives the tap -- not whatever
        # button happens to be underneath at that screen position. This is
        # what makes the wake-tap inert instead of click-through.
        self.sleep_overlay = tk.Frame(self, bg="black")
        self.sleep_overlay.place(x=0, y=0, relwidth=1, relheight=1)
        self.sleep_overlay.lift()
        self.sleep_overlay.bind("<Button-1>", lambda e: self._wake_up())
        self.sleep_overlay.focus_set()


    def _wake_up(self):
        self.is_sleeping = False
        self._run_wlopm("--on", "*")
        if self.sleep_overlay is not None:
            self.sleep_overlay.destroy()
            self.sleep_overlay = None
        self._reset_idle_timer()

    def hardware_daemon(self):
        while self.daemon_running:
            try:
                with SMBus(I2C_BUS) as bus:
                    data = bus.read_i2c_block_data(NANO_ADDR, 0, 8)
                    w_str = "".join(chr(c) for c in data if 32 <= c <= 126).strip()
                    clean = "".join(c for c in w_str if c.isdigit() or c == '.' or c == '-')
                    if clean: self.raw_weight = float(clean)
            except: pass
            time.sleep(0.15)

    def update_ui_loop(self):
        if not self.ui_loop_active: return
        if self.is_manual_mode:
            self.w_lbl.config(text=f"{self.last_weight:.2f} kg", fg="#F39C12")
            self.status_lbl.config(text="MANUAL ENTRY (Modified)")
            self.active_after_id = self.after(200, self.update_ui_loop)
            return

        try:
            if self.is_held:
                self.w_lbl.config(text=f"{self.last_weight:.2f} kg", fg="#E74C3C")
                self.status_lbl.config(text="WEIGHT HELD")
            else:
                current_w = max(0, (self.raw_weight / 1000.0) - self.tare_val)
                self.w_lbl.config(text=f"{current_w:.2f} kg", fg="#2ECC71")
                self.last_weight = current_w
                
                if abs(current_w - self.last_stable_weight) < 0.05 and current_w > 0.05:
                    elapsed = time.time() - self.stable_start_time
                    self.countdown = max(0, HOLD_DELAY - int(elapsed))
                    if self.countdown > 0:
                        self.status_lbl.config(text=f"Stable in {self.countdown}...")
                    if elapsed >= HOLD_DELAY:
                        self.is_held = True
                        self.status_lbl.config(text="WEIGHT HELD")
                else: 
                    self.last_stable_weight = current_w
                    self.stable_start_time = time.time()
                    self.status_lbl.config(text="")
        except: pass
        self.active_after_id = self.after(200, self.update_ui_loop)

    def perform_tare(self):
        self.tare_val = self.raw_weight / 1000.0
        self.is_held = False
        self.is_manual_mode = False
        self.stable_start_time = time.time()
        self.status_lbl.config(text="TARED")

    def _clear(self, show_nav=False):
        self.ui_loop_active = False
        if self.active_after_id: self.after_cancel(self.active_after_id)
        for w in self.winfo_children(): w.destroy()
        self.sleep_overlay = None  # any prior overlay was just destroyed above too
        if show_nav:
            nav_bar = tk.Frame(self, bg="#F4F7F6", height=90, highlightthickness=0)
            nav_bar.pack(side="top", fill="x")
            nav_bar.pack_propagate(False)
            tk.Button(nav_bar, image=self.icon_history, bg="#F4F7F6", bd=0, command=self.show_history_screen).pack(side="left", padx=25, pady=10)
            tk.Button(nav_bar, image=self.icon_settings, bg="#F4F7F6", bd=0, command=self.show_admin_menu).pack(side="right", padx=25, pady=10)
            nav_bar.lift()

    def _apply_touch_scroll(self, canvas, scroll_frame):
        def start_scroll(e): canvas.scan_mark(e.x, e.y)
        def do_scroll(e): canvas.scan_dragto(e.x, e.y, gain=2)
        canvas.bind("<Button-1>", start_scroll); canvas.bind("<B1-Motion>", do_scroll)
        scroll_frame.bind("<Button-1>", start_scroll); scroll_frame.bind("<B1-Motion>", do_scroll)

    def create_floating_keyboard(self, target, rel_y):
        btns = [['1', '2', '3'], ['4', '5', '6'], ['7', '8', '9'], ['<', '0', 'CLR']]
        start_x, x_step, y_step = 0.5, 0.18, 0.13 
        for r_idx, row in enumerate(btns):
            for c_idx, key in enumerate(row):
                tk.Button(self, text=key, font=("Arial", 26, "bold"), width=5, height=1, bg="#FFF4E0", relief="flat", 
                          command=lambda k=key: self.kb_press(k, target)).place(relx=start_x+(c_idx-1)*x_step, rely=rel_y+(r_idx-1.5)*y_step, anchor="center")

    def kb_press(self, k, t):
        if k == 'CLR': t.delete(0, tk.END)
        elif k == '<': t.delete(len(t.get())-1, tk.END)
        else: t.insert(tk.END, k)

    def show_customer_login(self):
        self._clear(show_nav=True)
        self.basket = []
        self.selected_hub = ""
        if self.bg_main:
            bg_l = tk.Label(self, image=self.bg_main); bg_l.place(x=0, y=0, relwidth=1, relheight=1); bg_l.lower()
        tk.Label(self, text="CUSTOMER LOGIN", font=("Arial", 36, "bold"), bg="#FFFFFF", fg="#27AE60").place(relx=0.5, rely=0.15, anchor="center")
        self.phone_ent = tk.Entry(self, font=("Arial", 32), justify='center', width=16, bd=0, bg="#FFFFFF")
        self.phone_ent.place(relx=0.5, rely=0.28, anchor="center")
        self.create_floating_keyboard(self.phone_ent, rel_y=0.58)
        tk.Button(self, text="CONTINUE", bg="#1E5128", fg="white", font=("Arial", 22, "bold"), height=2, width=22, command=self.auth_customer).place(relx=0.5, rely=0.92, anchor="center")

    def is_internet_fast_check(self):
        try:
            socket.setdefaulttimeout(1.5)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
            return True
        except socket.error:
            return False

    def auth_customer(self):
        ph = self.phone_ent.get().strip()
        if not ph: return
        
        if not self.is_internet_fast_check():
            self.show_network_error_ui(retry_callback=self.auth_customer)
            return

        try:
            resp = requests.get(f"https://app.ecobarter.africa/api/pos/get-user?phone={ph}", timeout=4).json()
            self.customer_name = resp["data"]["name"] if resp.get("success") else "New Customer"
        except: 
            self.show_network_error_ui(retry_callback=self.auth_customer)
            return
        
        self.customer_phone = ph
        self.session_id = db_module.create_session_id()
        self.show_welcome_screen()

    def show_network_error_ui(self, retry_callback):
        self._clear()
        tk.Label(self, text="CONNECTION ERROR", font=("Arial", 28, "bold"), bg="#F4F7F6", fg="#C0392B").pack(pady=60)
        tk.Label(self, text="The system requires an active internet connection.\nPlease check network signals and try again.", font=("Arial", 16), bg="#F4F7F6", fg="#7F8C8D").pack(pady=20)
        
        btn_frame = tk.Frame(self, bg="#F4F7F6")
        btn_frame.pack(pady=40)
        
        tk.Button(btn_frame, text="RETRY", bg="#27AE60", fg="white", font=("Arial", 16, "bold"), width=15, height=2, command=retry_callback).pack(side="left", padx=20)
        tk.Button(btn_frame, text="CANCEL", bg="#95A5A6", fg="white", font=("Arial", 16, "bold"), width=15, height=2, command=self.show_customer_login).pack(side="left", padx=20)

    def show_welcome_screen(self):
        self._clear(show_nav=False)
        f = tk.Frame(self, bg="white", padx=40, pady=40); f.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(f, text=f"Welcome,\n{self.customer_name}", font=("Arial", 28, "bold"), bg="white").pack(pady=20)
        btn_f = tk.Frame(f, bg="white"); btn_f.pack(pady=20)
        tk.Button(btn_f, text="Doorstep Pickup", bg="#3498DB", fg="white", font=("Arial", 16, "bold"), width=18, height=4, command=lambda: self.set_type("Doorstep Pickup")).pack(side="left", padx=15)
        tk.Button(btn_f, text="Drop Off", bg="#2ECC71", fg="white", font=("Arial", 16, "bold"), width=15, height=4, command=self.show_hub_selection).pack(side="left", padx=15)
        tk.Button(f, text="CANCEL", bg="#95A5A6", fg="white", width=10, command=self.show_customer_login).pack(pady=20)

    def set_type(self, t):
        self.request_type = t
        self.selected_hub = ""
        self.show_materials()

    def show_hub_selection(self):
        self._clear(show_nav=False)
        self.request_type = "Drop Off"
        
        header = tk.Frame(self, bg="#FFFFFF", height=80)
        header.pack(fill="x")
        tk.Button(header, text="← BACK", font=("Arial", 12, "bold"), command=self.show_welcome_screen).pack(side="left", padx=15, pady=15)
        tk.Label(header, text="SELECT A HUB", font=("Arial", 22, "bold"), bg="#FFFFFF", fg="#2C3E50").pack(pady=15)
        
        container = tk.Frame(self, bg="#F4F7F6")
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg="#F4F7F6", highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        
        scroll_frame = tk.Frame(canvas, bg="#F4F7F6")
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw", width=self.scr_w)
        
        self._apply_touch_scroll(canvas, scroll_frame)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        
        def drag_canvas(e): canvas.scan_dragto(e.x, e.y, gain=2)
        def mark_canvas(e): canvas.scan_mark(e.x, e.y)

        grid_frame = tk.Frame(scroll_frame, bg="#F4F7F6")
        grid_frame.pack(pady=15, padx=25, fill="both", expand=True)
        
        cols = 2
        for idx, hub in enumerate(self.hubs_list):
            btn = tk.Button(grid_frame, text=hub, font=("Arial", 15, "bold"), height=3, bg="white", fg="#34495E", activebackground="#2ECC71", activeforeground="white", relief="groove", bd=1,
                            command=lambda h=hub: self.select_hub_and_continue(h))
            btn.grid(row=idx//cols, column=idx%cols, padx=12, pady=10, sticky="nsew")
            btn.bind("<Button-1>", mark_canvas, add="+")
            btn.bind("<B1-Motion>", drag_canvas, add="+")
            grid_frame.grid_columnconfigure(idx%cols, weight=1)

    def select_hub_and_continue(self, hub_name):
        self.selected_hub = hub_name
        self.show_materials()

    def show_materials(self):
        self._clear(show_nav=False)
        header = tk.Frame(self, bg="#FFFFFF", height=80); header.pack(fill="x")
        
        back_cmd = self.show_hub_selection if self.request_type == "Drop Off" else self.show_welcome_screen
        tk.Button(header, text="← BACK", font=("Arial", 12, "bold"), command=back_cmd).pack(side="left", padx=15, pady=15)
        
        if self.basket: 
            tk.Button(header, text=f"FINISH ({len(self.basket)})", bg="#F39C12", font=("Arial", 12, "bold"), command=self.handle_finish).pack(side="right", padx=15, pady=15)
        container = tk.Frame(self, bg="#F4F7F6"); container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg="#F4F7F6", highlightthickness=0); canvas.pack(side="left", fill="both", expand=True)
        scroll_frame = tk.Frame(canvas, bg="#F4F7F6"); canvas.create_window((0, 0), window=scroll_frame, anchor="nw", width=self.scr_w)
        
        self._apply_touch_scroll(canvas, scroll_frame)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        
        def drag_canvas(e): canvas.scan_dragto(e.x, e.y, gain=2)
        def mark_canvas(e): canvas.scan_mark(e.x, e.y)

        for m in self.materials_list:
            btn = tk.Button(scroll_frame, text=m["display"], font=("Arial", 18, "bold"), height=2, bg="white", command=lambda d=m["display"], s=m["system"]: self.show_weighing(d, s))
            btn.pack(pady=4, padx=25, fill="x")
            btn.bind("<Button-1>", mark_canvas, add="+")
            btn.bind("<B1-Motion>", drag_canvas, add="+")

    def show_weighing(self, d_name, s_name):
        self._clear(show_nav=False)
        self.selected_material_display, self.selected_material_system = d_name, s_name
        self.stable_start_time, self.is_held, self.is_manual_mode, self.ui_loop_active, self.countdown = time.time(), False, False, True, HOLD_DELAY
        
        tk.Label(self, text=f"Weighing: {d_name}", font=("Arial", 22, "bold"), bg="#F4F7F6").pack(pady=10)
        self.w_lbl = tk.Label(self, text="0.00 kg", font=("Arial", 75, "bold"), bg="#F4F7F6", fg="#2ECC71")
        self.w_lbl.pack(pady=10)
        self.status_lbl = tk.Label(self, text="", font=("Arial", 16), bg="#F4F7F6", fg="#7F8C8D")
        self.status_lbl.pack(pady=5)
        
        btn_f = tk.Frame(self, bg="#F4F7F6"); btn_f.pack(side="bottom", pady=25)
        tk.Button(btn_f, text="BACK", bg="#95A5A6", fg="white", font=("Arial", 14, "bold"), width=10, height=3, command=self.show_materials).pack(side="left", padx=10)
        tk.Button(btn_f, text="TARE", font=("Arial", 14, "bold"), width=10, height=3, command=self.perform_tare).pack(side="left", padx=10)
        tk.Button(btn_f, text="MODIFY", bg="#F39C12", fg="white", font=("Arial", 14, "bold"), width=10, height=3, command=self.manual_weight_entry).pack(side="left", padx=10)
        tk.Button(btn_f, text="ADD", bg="#27AE60", fg="white", font=("Arial", 16, "bold"), width=15, height=3, command=self.add_to_basket).pack(side="left", padx=10)
        
        self.update_ui_loop()

    def add_to_basket(self):
        if self.last_weight > 0.01:
            self.basket.append({"item_type": self.selected_material_system, "display_name": self.selected_material_display, "weight": round(self.last_weight, 2)})
            self.is_manual_mode = False 
            self.show_materials()

    def handle_finish(self):
        self.ui_loop_active = False; self._clear()
        
        if not self.is_internet_fast_check():
            self.show_network_error_ui(retry_callback=self.handle_finish)
            return

        tk.Label(self, text="Syncing...", font=("Arial", 22, "bold"), bg="#F4F7F6", fg="#3498DB").pack(pady=150)
        now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        items_p = [
            {
                "item_type": str(i["item_type"]), 
                "weight": float(i["weight"]), 
                "weight_awarded": float(i["weight"])
            } 
            for i in self.basket
        ]
        
        self.current_payload = {
            "machine_name": f"SmartScale-{self.agent_id}", 
            "session_id": str(self.session_id), 
            "owner": str(self.customer_phone), 
            "agent": str(self.agent_id), 
            "request_type": str(self.request_type), 
            "recycled_at": now_ts, 
            "items": items_p
        }
        
        if self.selected_hub:
            self.current_payload["hub"] = self.selected_hub
        
        self.log_transaction()
        threading.Thread(target=self.threaded_sync, daemon=True).start()

    def threaded_sync(self):
        try:
            resp = requests.post("https://app.ecobarter.africa/api/pos", json=self.current_payload, timeout=5)
            print(f"[SYNC DEBUG] status_code={resp.status_code} raw_body={resp.text!r}")
            try:
                data = resp.json()
            except ValueError:
                data = {}
            print(f"[SYNC DEBUG] parsed_json={data!r}")

            # As with redeem, the body's success flag is the real source of
            # truth -- confirmed the backend returns 400 for business-logic
            # failures (e.g. insufficient balance). Show the real message
            # for any response that has one, regardless of status code.
            raw_success = data.get("success")
            success_flag = raw_success is True or str(raw_success).strip().lower() in ("true", "1")
            api_success = success_flag and resp.status_code in [200, 201]
            api_message = data.get("message", "").strip()

            if api_success:
                self.after(0, self.show_claim_options)
            elif api_message:
                msg = api_message
                self.after(0, lambda: self.show_redeem_failed(msg, retry_callback=self.handle_finish))
            else:
                self.after(0, lambda: self.show_network_error_ui(retry_callback=self.handle_finish))
        except: 
            self.after(0, lambda: self.show_network_error_ui(retry_callback=self.handle_finish))

    def show_claim_options(self):
        self._clear(); total_w = sum(i['weight'] for i in self.basket)
        qr_link = f"https://app.ecobarter.africa/recycle?payload={self.session_id}"
        qr = qrcode.QRCode(version=1, box_size=8, border=1); qr.add_data(qr_link); qr.make(fit=True)
        self.qr_img = ImageTk.PhotoImage(qr.make_image().resize((280, 280)))
        tk.Label(self, text="TRANSACTION SUCCESSFUL", font=("Arial", 22, "bold"), bg="#F4F7F6", fg="#27AE60").pack(pady=20)
        tk.Label(self, image=self.qr_img, bg="white").pack(pady=10)
        btn_f = tk.Frame(self, bg="#F4F7F6"); btn_f.pack(pady=30)
        
        tk.Button(btn_f, text="PRINT RECEIPT", bg="#3498DB", fg="white", font=("Arial", 14, "bold"), width=15, height=3, command=lambda: self.do_print(self.basket, qr_link)).pack(side="left", padx=15)
        tk.Button(btn_f, text="SEND POINTS", bg="#27AE60", fg="white", font=("Arial", 14, "bold"), width=15, height=3, command=self.do_send_point).pack(side="left", padx=15)

    def do_send_point(self):
        self._clear()
        if not self.is_internet_fast_check():
            self.show_network_error_ui(retry_callback=self.do_send_point)
            return
            
        tk.Label(self, text="Processing Points...", font=("Arial", 24, "bold"), bg="#F4F7F6", fg="#3498DB").pack(pady=150)
        threading.Thread(target=self.threaded_redeem, daemon=True).start()

    def threaded_redeem(self):
        try:
            redeem_url = f"https://app.ecobarter.africa/api/pos/redeem?payload={self.session_id}&phone={self.customer_phone}&action=credit"
            if self.selected_hub:
                redeem_url += f"&hub={requests.utils.quote(self.selected_hub)}"
                
            resp = requests.post(redeem_url, timeout=5)
            print(f"[REDEEM DEBUG] status_code={resp.status_code} raw_body={resp.text!r}")
            try:
                data = resp.json()
            except ValueError:
                data = {}
            print(f"[REDEEM DEBUG] parsed_json={data!r}")

            # The API can return success:false on various status codes --
            # confirmed 400 for insufficient balance. The real source of
            # truth is the success flag in the body. If the response has
            # a parseable message, always show it regardless of status code.
            raw_success = data.get("success")
            success_flag = raw_success is True or str(raw_success).strip().lower() in ("true", "1")
            api_success = success_flag and resp.status_code in [200, 201]
            api_message = data.get("message", "").strip()

            if api_success:
                self.after(0, self.show_thank_you)
            elif api_message:
                # Server responded with a real explanation -- show it
                # regardless of whether the status code was 200, 400, etc.
                msg = api_message
                self.after(0, lambda: self.show_redeem_failed(msg))
            else:
                # No message in the body -- genuine connectivity / server error
                self.after(0, lambda: self.show_network_error_ui(retry_callback=self.do_send_point))
        except Exception as e:
            self.after(0, lambda: self.show_network_error_ui(retry_callback=self.do_send_point))

    def show_redeem_failed(self, message, retry_callback=None):
        self._clear()
        if retry_callback is None:
            retry_callback = self.do_send_point
        f = tk.Frame(self, bg="#F4F7F6", padx=40, pady=40)
        f.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(f, text="TRANSACTION NOT COMPLETED", font=("Arial", 22, "bold"), bg="#F4F7F6", fg="#C0392B").pack(pady=15)
        tk.Label(f, text=message, font=("Arial", 16), bg="#F4F7F6", fg="#34495E", wraplength=600, justify="center").pack(pady=15)
        btn_f = tk.Frame(f, bg="#F4F7F6")
        btn_f.pack(pady=25)
        tk.Button(btn_f, text="TRY AGAIN", bg="#3498DB", fg="white", font=("Arial", 14, "bold"), width=14, height=2,
                  command=retry_callback).pack(side="left", padx=10)
        tk.Button(btn_f, text="BACK TO START", bg="#95A5A6", fg="white", font=("Arial", 14, "bold"), width=14, height=2,
                  command=self.show_customer_login).pack(side="left", padx=10)

    def do_print(self, basket_items, q, point_mode=False):
        if printer:
            try:
                total_w = sum(i['weight'] for i in basket_items)
                printer.set(align='center', bold=True); printer.text("Ecobarter POS\n")
                if point_mode: printer.text("POINTS CREDITED TO WALLET\n")
                
                printer.set(align='left', bold=False)
                printer.text(f"Customer Name: {self.customer_name}\n")
                printer.text(f"Phone Number:   {self.customer_phone}\n")
                if self.selected_hub:
                    printer.text(f"Hub Location:   {self.selected_hub}\n")
                printer.text(f"Date:          {datetime.date.today()}\n")
                printer.text("-" * 32 + "\n") 
                
                for item in basket_items:
                    display_line = f"{item['display_name'][:20]:<22} {item['weight']:.2f}kg\n"
                    printer.text(display_line)
                    
                printer.text("-" * 32 + "\n")
                printer.set(bold=True)
                printer.text(f"TOTAL WEIGHT:           {total_w:.2f}kg\n")
                printer.set(bold=False)
                
                if q: 
                    printer.set(align='center')
                    printer.qr(q, size=8, native=True)
                    
                printer.set(align='center')
                printer.text("\nThank you for recycling!\n")
                printer.cut()
                
                self.after(0, self.show_thank_you)
            except Exception as e:
                print(f"Printing failed: {e}")

    def show_thank_you(self):
        self._clear(); f = tk.Frame(self, bg="#27AE60"); f.place(relwidth=1, relheight=1)
        tk.Label(f, text="THANK YOU!", font=("Arial", 48, "bold"), fg="white", bg="#27AE60").place(relx=0.5, rely=0.4, anchor="center")
        tk.Label(f, text="Transaction Processed Successfully", font=("Arial", 20), fg="white", bg="#27AE60").place(relx=0.5, rely=0.52, anchor="center")
        tk.Label(f, text="Returning to home in 5 seconds...", font=("Arial", 14), fg="white", bg="#27AE60").place(relx=0.5, rely=0.62, anchor="center")
        self.after(5000, self.show_customer_login)

    def show_history_screen(self):
        self._clear(); top = tk.Frame(self, bg="#FFFFFF", height=80); top.pack(fill="x")
        tk.Button(top, text="← BACK", font=("Arial", 14, "bold"), command=self.show_customer_login).pack(side="left", padx=20, pady=15)
        container = tk.Frame(self, bg="#F4F7F6"); container.pack(fill="both", expand=True, padx=20, pady=10)
        canvas = tk.Canvas(container, bg="#F4F7F6", highlightthickness=0); canvas.pack(side="left", fill="both", expand=True)
        scroll_frame = tk.Frame(canvas, bg="#F4F7F6"); canvas.create_window((0, 0), window=scroll_frame, anchor="nw", width=self.scr_w-60)
        self._apply_touch_scroll(canvas, scroll_frame); scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
                logs = json.load(f)
                for idx, l in enumerate(logs):
                    row = tk.Frame(scroll_frame, bg="white", pady=10); row.pack(fill="x", pady=6, padx=10)
                    tk.Label(row, text=f"{l.get('date')} | {l.get('customer')}", font=("Arial", 14, "bold"), bg="white").pack(side="left", padx=20)
                    tk.Button(row, text="DELETE", bg="#E74C3C", fg="white", font=("Arial", 10, "bold"), command=lambda transaction_id=l.get('id'): self.delete_history_record(transaction_id)).pack(side="right", padx=10)
                    tk.Button(row, text="DETAILS >", bg="#3498DB", fg="white", command=lambda d=l: self.show_history_detail(d)).pack(side="right", padx=10)

    def delete_history_record(self, transaction_id):
        if not transaction_id:
            return
        # Guard against double-tap on touchscreens re-opening the modal
        # while the first one is still alive underneath it.
        if hasattr(self, 'delete_modal') and self.delete_modal.winfo_exists():
            return

        # Pure Tkinter custom overlay confirmation UI modal window
        self.delete_modal = tk.Frame(self, bg="#F4F7F6")
        self.delete_modal.place(x=0, y=0, relwidth=1, relheight=1)
        
        box = tk.Frame(self.delete_modal, bg="white", padx=30, pady=30, highlightbackground="#BDC3C7", highlightthickness=1)
        box.place(relx=0.5, rely=0.5, anchor="center")
        
        tk.Label(box, text="CONFIRM DELETE", font=("Arial", 22, "bold"), bg="white", fg="#C0392B").pack(pady=10)
        tk.Label(box, text="Are you sure you want to delete\nthis log record permanently?", font=("Arial", 14), bg="white", fg="#34495E").pack(pady=15)
        
        btn_f = tk.Frame(box, bg="white")
        btn_f.pack(pady=15)
        
        cancel_btn = tk.Button(btn_f, text="CANCEL", bg="#95A5A6", fg="white", font=("Arial", 12, "bold"), width=10, height=2,
                                command=self.close_delete_modal)
        cancel_btn.pack(side="left", padx=10)

        delete_btn = tk.Button(btn_f, text="DELETE", bg="#E74C3C", fg="white", font=("Arial", 12, "bold"), width=10, height=2,
                                command=lambda: self.execute_log_deletion(transaction_id, delete_btn, cancel_btn))
        delete_btn.pack(side="left", padx=10)

    def close_delete_modal(self):
        if hasattr(self, 'delete_modal'):
            try:
                if self.delete_modal.winfo_exists():
                    self.delete_modal.destroy()
            except tk.TclError:
                pass
            finally:
                del self.delete_modal

    def execute_log_deletion(self, transaction_id, delete_btn=None, cancel_btn=None):
        # Disable both buttons immediately so a second tap during the
        # (potentially slow, SD-card-bound) file I/O can't re-enter this
        # function or call close_delete_modal mid-write.
        if delete_btn is not None:
            delete_btn.config(state="disabled", text="DELETING...")
        if cancel_btn is not None:
            cancel_btn.config(state="disabled")
        self.update_idletasks()  # force the disabled state to paint before blocking I/O

        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f:
                    logs = json.load(f)
                filtered_logs = [entry for entry in logs if entry.get('id') != transaction_id]
                with open(LOG_FILE, "w") as f:
                    json.dump(filtered_logs, f)
            except Exception as e:
                print(f"Failed to clear tracking log record: {e}")
                
        self.close_delete_modal()
        self.show_history_screen()

    def show_history_detail(self, data):
        self._clear()
        header = tk.Frame(self, bg="white", height=80)
        header.pack(fill="x")
        tk.Button(header, text="← BACK", command=self.show_history_screen).pack(side="left", padx=20, pady=15)
        f = tk.Frame(self, bg="white", padx=50, pady=30)
        f.pack(fill="both", expand=True)
        tk.Label(f, text=f"Customer: {data.get('customer')}", font=("Arial", 18, "bold"), bg="white").pack(anchor="w")
        for item in data.get('items', []):
            tk.Label(f, text=f"• {item.get('display_name')}: {item.get('weight')} kg", bg="white").pack(anchor="w", padx=20)
        tk.Label(f, text=f"TOTAL: {data.get('total')} kg", font=("Arial", 22, "bold"), fg="#27AE60", bg="white").pack(anchor="w", pady=40)

    def manual_weight_entry(self):
        self.modal_overlay = tk.Frame(self, bg="#F4F7F6")
        self.modal_overlay.place(x=0, y=0, relwidth=1, relheight=1)

        tk.Label(self.modal_overlay, text="Enter Manual Weight (kg):", font=("Arial", 18, "bold"), bg="#F4F7F6", fg="#2C3E50").pack(pady=15)

        ent = tk.Entry(self.modal_overlay, font=("Arial", 24), justify='center', width=12, bd=2, relief="groove")
        ent.pack(pady=5)
        ent.focus_set()

        kf = tk.Frame(self.modal_overlay, bg="#F4F7F6")
        kf.pack(pady=10)

        keys = [
            '1', '2', '3',
            '4', '5', '6',
            '7', '8', '9',
            '<', '0', '.'
        ]

        for i, k in enumerate(keys):
            cmd_action = lambda x=k: ent.delete(len(ent.get())-1, tk.END) if x == '<' else ent.insert(tk.END, x)
            tk.Button(kf, text=k, width=5, height=1, font=("Arial", 14, "bold"),
                      bg="white", fg="#34495E", activebackground="#3498DB", activeforeground="white",
                      command=cmd_action).grid(row=i//3, column=i%3, padx=6, pady=6)

        btn_frame = tk.Frame(self.modal_overlay, bg="#F4F7F6")
        btn_frame.pack(pady=15, fill="x", padx=40)

        tk.Button(btn_frame, text="CANCEL", bg="#95A5A6", fg="white", font=("Arial", 14, "bold"), width=10, height=2,
                  command=self.close_manual_entry).pack(side="left", padx=10, expand=True)

        tk.Button(btn_frame, text="CONFIRM", bg="#27AE60", fg="white", font=("Arial", 14, "bold"), width=12, height=2,
                  command=lambda: self.set_manual(ent.get())).pack(side="right", padx=10, expand=True)

    def close_manual_entry(self):
        if hasattr(self, 'modal_overlay'):
            self.modal_overlay.destroy()

    def set_manual(self, val):
        try:
            parsed_val = float(val)
            if parsed_val >= 0:
                self.last_weight = parsed_val
                self.is_manual_mode = True
                self.close_manual_entry()

                self.w_lbl.config(text=f"{self.last_weight:.2f} kg", fg="#F39C12")
                self.status_lbl.config(text="MANUAL ENTRY (Modified)")
        except ValueError:
            pass

    def log_transaction(self):
        total_w = sum(i['weight'] for i in self.basket)
        entry = {"id": self.session_id, "agent": self.agent_id, "customer": self.customer_name, "phone": self.customer_phone, "total": round(total_w, 2), "items": list(self.basket), "date": str(datetime.date.today())}
        if self.selected_hub:
            entry["hub"] = self.selected_hub
        logs = []
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f: logs = json.load(f)
            except: pass
        logs.insert(0, entry)
        with open(LOG_FILE, "w") as f: json.dump(logs[:50], f)

        try:
            now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in self.basket:
                db_module.insert_or_update_values(
                    session_id=str(self.session_id),
                    machine_name=f"SmartScale-{self.agent_id}",
                    owner=str(self.customer_phone),
                    agent=str(self.agent_id),
                    request_type=str(self.request_type),
                    item_type=str(item["item_type"]),
                    weight=float(item["weight"]),
                    weight_awarded=float(item["weight"]),
                    recycled_at=now_ts
                )
        except Exception as e:
            print(f"Error executing db_module transaction mapping loop: {e}")

    def load_agent_id(self):
        if os.path.exists(AGENT_FILE):
            with open(AGENT_FILE, "r") as f: return f.read().strip()
        return None

    def save_agent(self, aid):
        with open(AGENT_FILE, "w") as f: f.write(aid.strip())
        self.agent_id = aid.strip(); self.show_customer_login()

    def show_agent_auth(self):
        self._clear()
        tk.Label(self, text="Agent Login", font=("Arial", 26, "bold"), bg="#F4F7F6").place(relx=0.5, rely=0.15, anchor="center")
        ent = tk.Entry(self, font=("Arial", 28), justify='center')
        ent.place(relx=0.5, rely=0.28, anchor="center")
        self.create_floating_keyboard(ent, rel_y=0.58)
        tk.Button(self, text="LOGIN", bg="#3498DB", fg="white", font=("Arial", 18, "bold"), height=2, width=15,
                  command=lambda: self.save_agent(ent.get())).place(relx=0.5, rely=0.92, anchor="center")

    def show_admin_menu(self):
        self._clear()
        tk.Label(self, text="ADMIN MENU", font=("Arial", 24, "bold"), bg="#F4F7F6").pack(pady=(40, 20))
        tk.Button(self, text="WI-FI SETTINGS", bg="#3498DB", fg="white", font=("Arial", 16, "bold"), width=20, height=3, command=self.show_wifi_settings).pack(pady=10)
        tk.Button(self, text="LOGOUT AGENT", bg="#E74C3C", fg="white", width=20, height=3, command=self.perform_logout).pack(pady=10)
        tk.Button(self, text="CANCEL", command=self.show_customer_login).pack(pady=20)

    def perform_logout(self):
        if os.path.exists(AGENT_FILE): os.remove(AGENT_FILE)
        self.agent_id = None; self.show_agent_auth()

    # =====================================================================
    # Wi-Fi settings -- lets an agent connect the Pi to a new network from
    # the touchscreen, without needing physical/desktop access. Uses nmcli
    # (NetworkManager). Requires a one-time sudoers entry on the Pi so this
    # user can run the specific nmcli commands below without a password
    # prompt -- see setup notes provided separately.
    # =====================================================================

    def show_wifi_settings(self):
        self._clear()
        header = tk.Frame(self, bg="#FFFFFF", height=80)
        header.pack(fill="x")
        tk.Button(header, text="← BACK", font=("Arial", 12, "bold"), command=self.show_admin_menu).pack(side="left", padx=15, pady=15)
        tk.Label(header, text="WI-FI SETTINGS", font=("Arial", 20, "bold"), bg="#FFFFFF", fg="#2C3E50").pack(side="left", padx=10)
        tk.Button(header, text="RESCAN", bg="#3498DB", fg="white", font=("Arial", 12, "bold"), command=self.show_wifi_settings).pack(side="right", padx=15, pady=15)

        self.wifi_status_lbl = tk.Label(self, text="Scanning for networks...", font=("Arial", 14), bg="#F4F7F6", fg="#7F8C8D")
        self.wifi_status_lbl.pack(pady=10)

        container = tk.Frame(self, bg="#F4F7F6")
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg="#F4F7F6", highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        self.wifi_list_frame = tk.Frame(canvas, bg="#F4F7F6")
        canvas.create_window((0, 0), window=self.wifi_list_frame, anchor="nw", width=self.scr_w)
        self._apply_touch_scroll(canvas, self.wifi_list_frame)
        self.wifi_list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        threading.Thread(target=self._threaded_wifi_scan, daemon=True).start()

    def _threaded_wifi_scan(self):
        networks = []
        try:
            # Force a fresh scan first so newly-in-range networks show up,
            # then list results in a stable, script-friendly format.
            subprocess.run(["nmcli", "device", "wifi", "rescan"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SECURITY,SIGNAL", "device", "wifi", "list"],
                capture_output=True, text=True, timeout=10
            )
            seen = set()
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(":")
                if len(parts) < 3:
                    continue
                ssid, security, signal = parts[0], parts[1], parts[2]
                if not ssid or ssid in seen:
                    continue  # skip hidden/blank SSIDs and de-duplicate repeated APs
                seen.add(ssid)
                try:
                    signal_val = int(signal)
                except ValueError:
                    signal_val = 0
                networks.append({"ssid": ssid, "secured": bool(security.strip()), "signal": signal_val})
            networks.sort(key=lambda n: n["signal"], reverse=True)
        except Exception as e:
            print(f"Wi-Fi scan failed: {e}")
        self.after(0, lambda: self._populate_wifi_list(networks))

    def _populate_wifi_list(self, networks):
        if not hasattr(self, 'wifi_list_frame') or not self.wifi_list_frame.winfo_exists():
            return  # user navigated away before the scan finished

        if not networks:
            self.wifi_status_lbl.config(text="No networks found. Tap RESCAN to try again.")
            return
        self.wifi_status_lbl.config(text=f"{len(networks)} network(s) found -- tap one to connect")

        for net in networks:
            lock = "🔒 " if net["secured"] else ""
            btn = tk.Button(self.wifi_list_frame, text=f"{lock}{net['ssid']}  ({net['signal']}%)",
                             font=("Arial", 16, "bold"), height=2, bg="white", anchor="w", padx=20,
                             command=lambda n=net: self.show_wifi_password_entry(n))
            btn.pack(pady=4, padx=25, fill="x")

    def show_wifi_password_entry(self, network):
        self._clear()

        if not network["secured"]:
            # Open network -- no password needed, just confirm and connect.
            f = tk.Frame(self, bg="white", padx=40, pady=40)
            f.place(relx=0.5, rely=0.5, anchor="center")
            tk.Label(f, text=network["ssid"], font=("Arial", 22, "bold"), bg="white", fg="#2C3E50").pack(pady=10)
            tk.Label(f, text="Open network -- no password needed.", font=("Arial", 14), bg="white", fg="#7F8C8D").pack(pady=10)
            btn_f = tk.Frame(f, bg="white"); btn_f.pack(pady=20)
            tk.Button(btn_f, text="CANCEL", bg="#95A5A6", fg="white", font=("Arial", 13, "bold"), width=10, height=2,
                      command=self.show_wifi_settings).pack(side="left", padx=10)
            connect_btn = tk.Button(btn_f, text="CONNECT", bg="#27AE60", fg="white", font=("Arial", 13, "bold"), width=10, height=2)
            connect_btn.config(command=lambda: self._attempt_wifi_connect(network, "", connect_btn))
            connect_btn.pack(side="left", padx=10)
            return

        # --- Secured network: full-screen layout with QWERTY keyboard ---
        header = tk.Frame(self, bg="#FFFFFF", height=70)
        header.pack(fill="x")
        tk.Button(header, text="← CANCEL", font=("Arial", 12, "bold"), command=self.show_wifi_settings).pack(side="left", padx=15, pady=12)
        tk.Label(header, text=network["ssid"], font=("Arial", 18, "bold"), bg="#FFFFFF", fg="#2C3E50").pack(side="left", padx=10)

        entry_frame = tk.Frame(self, bg="#F4F7F6")
        entry_frame.pack(fill="x", pady=15)

        self.wifi_pw_visible = False
        ent = tk.Entry(entry_frame, font=("Arial", 22), justify="center", show="•", bd=2, relief="groove")
        ent.pack(side="left", padx=(40, 10), fill="x", expand=True)
        ent.focus_set()

        def toggle_visibility():
            self.wifi_pw_visible = not self.wifi_pw_visible
            ent.config(show="" if self.wifi_pw_visible else "•")
            show_btn.config(text="HIDE" if self.wifi_pw_visible else "SHOW")

        show_btn = tk.Button(entry_frame, text="SHOW", font=("Arial", 11, "bold"), command=toggle_visibility)
        show_btn.pack(side="left", padx=(0, 40))

        connect_btn = tk.Button(self, text="CONNECT", bg="#27AE60", fg="white", font=("Arial", 14, "bold"), height=2)
        connect_btn.config(command=lambda: self._attempt_wifi_connect(network, ent.get(), connect_btn))
        connect_btn.pack(fill="x", padx=40, pady=(0, 10))

        self.create_qwerty_keyboard(ent)

    def create_qwerty_keyboard(self, target):
        """Full touchscreen QWERTY keyboard with shift and a symbols row, for Wi-Fi
        passwords and anywhere else free-text entry (not just digits) is needed."""
        self.kb_shift = False

        kb_frame = tk.Frame(self, bg="#DADFE1")
        kb_frame.pack(side="bottom", fill="x", pady=4)

        rows_lower = [
            list("1234567890"),
            list("qwertyuiop"),
            list("asdfghjkl"),
            list("zxcvbnm"),
        ]
        rows_upper = [
            list("!@#$%^&*()"),
            list("QWERTYUIOP"),
            list("ASDFGHJKL"),
            list("ZXCVBNM"),
        ]

        self.kb_key_buttons = []  # (button, lower_char, upper_char) so SHIFT can relabel them live

        def make_row(parent, lower_chars, upper_chars):
            row_f = tk.Frame(parent, bg="#DADFE1")
            row_f.pack(pady=2)
            for lo, up in zip(lower_chars, upper_chars):
                b = tk.Button(row_f, text=lo, font=("Arial", 14, "bold"), width=3, height=2,
                              bg="white", relief="raised",
                              command=lambda lo=lo, up=up: self._qwerty_press(target, up if self.kb_shift else lo))
                b.pack(side="left", padx=2)
                self.kb_key_buttons.append((b, lo, up))
            return row_f

        for lo_row, up_row in zip(rows_lower, rows_upper):
            make_row(kb_frame, lo_row, up_row)

        bottom_row = tk.Frame(kb_frame, bg="#DADFE1")
        bottom_row.pack(pady=2)

        self.shift_btn = tk.Button(bottom_row, text="SHIFT", font=("Arial", 12, "bold"), width=6, height=2,
                                    bg="#F39C12", fg="white", command=lambda: self._toggle_shift())
        self.shift_btn.pack(side="left", padx=2)

        tk.Button(bottom_row, text="SPACE", font=("Arial", 12, "bold"), width=14, height=2,
                  command=lambda: self._qwerty_press(target, " ")).pack(side="left", padx=2)

        tk.Button(bottom_row, text="⌫", font=("Arial", 14, "bold"), width=5, height=2,
                  bg="#E74C3C", fg="white",
                  command=lambda: target.delete(len(target.get())-1, tk.END)).pack(side="left", padx=2)

        tk.Button(bottom_row, text="CLEAR", font=("Arial", 12, "bold"), width=6, height=2,
                  bg="#95A5A6", fg="white",
                  command=lambda: target.delete(0, tk.END)).pack(side="left", padx=2)

    def _qwerty_press(self, target, char):
        target.insert(tk.END, char)

    def _toggle_shift(self):
        self.kb_shift = not self.kb_shift
        self.shift_btn.config(bg="#D35400" if self.kb_shift else "#F39C12")
        for btn, lo, up in self.kb_key_buttons:
            btn.config(text=up if self.kb_shift else lo)

    def _attempt_wifi_connect(self, network, password, connect_btn):
        if network["secured"] and not password:
            return  # require a password for secured networks before attempting

        connect_btn.config(state="disabled", text="CONNECTING...")
        self.update_idletasks()

        threading.Thread(target=self._threaded_wifi_connect, args=(network, password), daemon=True).start()

    def _threaded_wifi_connect(self, network, password):
        ssid = network["ssid"]
        try:
            if network["secured"]:
                cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid, "password", password]
            else:
                cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            success = (result.returncode == 0)
            error_text = (result.stderr or result.stdout or "Unknown error").strip()
        except subprocess.TimeoutExpired:
            success, error_text = False, "Connection attempt timed out."
        except Exception as e:
            success, error_text = False, str(e)

        self.after(0, lambda: self._on_wifi_connect_result(success, ssid, error_text))

    def _on_wifi_connect_result(self, success, ssid, error_text):
        self._clear()
        box = tk.Frame(self, bg="white", padx=30, pady=30)
        box.place(relx=0.5, rely=0.5, anchor="center")

        if success:
            tk.Label(box, text="CONNECTED", font=("Arial", 22, "bold"), bg="white", fg="#27AE60").pack(pady=10)
            tk.Label(box, text=f"Successfully connected to:\n{ssid}", font=("Arial", 14), bg="white", fg="#34495E").pack(pady=10)
            tk.Button(box, text="DONE", bg="#27AE60", fg="white", font=("Arial", 14, "bold"), width=14, height=2,
                      command=self.show_admin_menu).pack(pady=15)
        else:
            # Connection failed -- nmcli does not tear down a previously
            # working connection on a failed attempt, so the Pi stays on
            # whatever network it had before. Just let the agent retry.
            tk.Label(box, text="CONNECTION FAILED", font=("Arial", 20, "bold"), bg="white", fg="#C0392B").pack(pady=10)
            tk.Label(box, text="Check the password and try again.", font=("Arial", 13), bg="white", fg="#7F8C8D").pack(pady=5)
            tk.Button(box, text="TRY AGAIN", bg="#E74C3C", fg="white", font=("Arial", 14, "bold"), width=14, height=2,
                      command=self.show_wifi_settings).pack(pady=15)


if __name__ == "__main__":
    app = ERPOSApp()
    app.mainloop()