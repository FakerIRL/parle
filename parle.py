"""
Parle — dictée vocale pour Windows.

Maintiens Ctrl + Espace, parle, relâche : le texte s'écrit là où se trouve ton curseur.
La transcription est faite sur ton PC avec Whisper (via faster-whisper), rien n'est envoyé sur internet.
"""

import ctypes
import gc
import json
import math
import os
import sys
import threading
import time
import traceback
import winreg
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

import keyboard
import numpy as np
import pyperclip
import pystray
import sounddevice as sd
from faster_whisper import WhisperModel
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageGrab


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

# Réglages par défaut. Ils sont recopiés dans %APPDATA%\Parle\config.json
# au premier lancement : c'est ce fichier qu'il faut modifier, pas le code.
REGLAGES_PAR_DEFAUT = {
    "actif": True,
    "micro": None,                  # None = micro par défaut de Windows
    "modele": "small",              # tiny, base, small, medium, large-v3-turbo...
    "langue": "fr",
    "vocabulaire": "",              # mots rares à bien orthographier, séparés par des virgules
    "garder_modele_secondes": 60,   # temps pendant lequel le modèle reste en mémoire après une dictée
}

FREQ = 16000            # Whisper travaille en 16 kHz
SCAN_ESPACE = 57        # codes physiques des touches (indépendants de la langue du clavier)
SCAN_CTRL = 29
SCAN_V = 47

DOSSIER = Path(os.environ["APPDATA"]) / "Parle"
CONFIG = DOSSIER / "config.json"
JOURNAL = DOSSIER / "erreurs.log"

# Clé du registre qui liste les programmes lancés au démarrage de Windows
CLE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
COMMANDE = f'"{Path(sys.prefix) / "Scripts" / "pythonw.exe"}" "{Path(__file__).resolve()}"'

# Couleurs de l'icône près de l'horloge et du point lumineux de la bulle
ICONES = {"off": "#5f6368", "pret": "#2e7d32", "ecoute": "#d32f2f", "transcription": "#f9a825"}
TEINTES = {"ecoute": (255, 69, 58), "transcription": (255, 189, 46), "fini": (48, 209, 88), "erreur": (255, 69, 58)}

# Hauteur relative des 7 barres de l'onde (plus hautes au centre)
PROFIL = [0.42, 0.68, 0.88, 1.0, 0.88, 0.68, 0.42]


# ---------------------------------------------------------------------------
# Accès à l'API Windows (pour la fenêtre transparente de la bulle)
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# Styles de la fenêtre : transparente, toujours au-dessus, sans icône dans la barre des tâches,
# qui laisse passer les clics et ne vole jamais le focus du clavier.
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_TOPMOST = 0x8
WS_EX_NOACTIVATE = 0x8000000
WDA_EXCLUDEFROMCAPTURE = 0x11


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


# Types des fonctions Windows utilisées (indispensable en 64 bits)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + \
    [ctypes.c_int] * 4 + [wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


# ---------------------------------------------------------------------------
# Petits outils
# ---------------------------------------------------------------------------

def journaliser(*infos):
    """Écrit une erreur dans %APPDATA%\\Parle\\erreurs.log (l'appli n'a pas de console)."""
    DOSSIER.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(time.strftime("%d/%m %H:%M:%S ") + "".join(traceback.format_exception(*infos)))


sys.excepthook = journaliser
threading.excepthook = lambda a: journaliser(a.exc_type, a.exc_value, a.exc_traceback)


def lire_config():
    try:
        enregistres = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        enregistres = {}
    return {**REGLAGES_PAR_DEFAUT, **enregistres}


def demarrage_actif():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_RUN) as cle:
            return winreg.QueryValueEx(cle, "Parle")[0] == COMMANDE
    except OSError:
        return False


def regler_demarrage(oui):
    """Ajoute ou retire l'appli des programmes lancés avec Windows."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_RUN, 0, winreg.KEY_SET_VALUE) as cle:
        if oui:
            winreg.SetValueEx(cle, "Parle", 0, winreg.REG_SZ, COMMANDE)
        else:
            try:
                winreg.DeleteValue(cle, "Parle")
            except OSError:
                pass


def est_wasapi(appareil):
    return "WASAPI" in sd.query_hostapis(appareil["hostapi"])["name"]


def micros():
    """Liste des micros. On passe par WASAPI, qui donne les noms complets des appareils."""
    return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0 and est_wasapi(d)]


def trouver_micro(nom):
    """Renvoie (numéro, infos) du micro choisi, ou du micro par défaut de Windows."""
    if nom:
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == nom and d["max_input_channels"] > 0 and est_wasapi(d):
                return i, d
    wasapi = next((h for h in sd.query_hostapis() if "WASAPI" in h["name"]), None)
    i = wasapi["default_input_device"] if wasapi and wasapi["default_input_device"] >= 0 else sd.default.device[0]
    return i, sd.query_devices(i)


def joindre(*morceaux):
    return " ".join(m for m in morceaux if m).strip()


def icone(couleur):
    """Dessine l'icône ronde avec un micro, affichée près de l'horloge."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dessin = ImageDraw.Draw(img)
    dessin.ellipse((4, 4, 60, 60), fill=couleur)
    dessin.rounded_rectangle((25, 14, 39, 38), radius=7, fill="white")
    dessin.arc((18, 24, 46, 46), 0, 180, fill="white", width=3)
    dessin.line((32, 46, 32, 52), fill="white", width=3)
    return img


def police(taille):
    for nom in ("seguisb.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(nom, taille)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Dessin de l'effet verre
# Les formes sont dessinées 3 fois plus grandes puis réduites : ça lisse les bords.
# Les résultats sont gardés en cache car ils ne changent que si la taille change.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def masque(l, h, r):
    """Forme d'un rectangle arrondi (blanc = visible)."""
    k = 3
    m = Image.new("L", (l * k, h * k), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, l * k - 1, h * k - 1), radius=r * k, fill=255)
    return m.resize((l, h), Image.LANCZOS)


@lru_cache(maxsize=64)
def degrade(l, h, haut, bas):
    """Dégradé vertical, de la valeur 'haut' à la valeur 'bas'."""
    colonne = np.linspace(haut, bas, h, dtype=np.float32)[:, None]
    return Image.fromarray(np.repeat(colonne, l, axis=1).astype(np.uint8), "L")


@lru_cache(maxsize=64)
def liseret(l, h, r, epaisseur):
    """Fin contour lumineux, plus brillant en haut, comme un reflet sur du verre."""
    k = 3
    m = Image.new("L", (l * k, h * k), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, l * k - 1, h * k - 1), radius=r * k, outline=255,
                                        width=max(1, int(epaisseur * k)))
    m = m.resize((l, h), Image.LANCZOS)
    return ImageChops.multiply(m, degrade(l, h, 200, 40))


@lru_cache(maxsize=64)
def ombre(l, h, r, flou):
    """Ombre douce sous un élément."""
    marge = flou * 3
    m = Image.new("L", (l + 2 * marge, h + 2 * marge), 0)
    ImageDraw.Draw(m).rounded_rectangle((marge, marge, marge + l, marge + h), radius=r, fill=90)
    calque = Image.new("RGBA", m.size, (0, 0, 0, 255))
    calque.putalpha(m.filter(ImageFilter.GaussianBlur(flou)))
    return calque, marge


@lru_cache(maxsize=16)
def halo(rayon, couleur):
    """Lueur colorée autour du point lumineux."""
    taille = rayon * 6
    img = Image.new("RGBA", (taille, taille), couleur + (0,))
    m = Image.new("L", (taille, taille), 0)
    c = taille // 2
    ImageDraw.Draw(m).ellipse((c - rayon * 1.6, c - rayon * 1.6, c + rayon * 1.6, c + rayon * 1.6), fill=150)
    img.putalpha(m.filter(ImageFilter.GaussianBlur(rayon)))
    return img


def poser(hwnd, img, x, y, opacite):
    """Affiche une image avec transparence dans la fenêtre de la bulle."""
    l, h = img.size
    # Windows attend des pixels en ordre Bleu-Vert-Rouge-Alpha, couleurs pré-multipliées par l'alpha
    a = np.asarray(img, dtype=np.uint16)
    alpha = (a[..., 3] * opacite).astype(np.uint16)
    bgra = np.empty((h, l, 4), dtype=np.uint8)
    bgra[..., 0] = a[..., 2] * alpha // 255
    bgra[..., 1] = a[..., 1] * alpha // 255
    bgra[..., 2] = a[..., 0] * alpha // 255
    bgra[..., 3] = alpha
    donnees = bgra.tobytes()

    ecran = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(ecran)
    info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), l, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, donnees, len(donnees))
    ancien = gdi32.SelectObject(dc, bmp)
    user32.UpdateLayeredWindow(hwnd, ecran, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(wintypes.SIZE(l, h)),
                               dc, ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2)
    gdi32.SelectObject(dc, ancien)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, ecran)


# ---------------------------------------------------------------------------
# La bulle affichée en bas de l'écran
# ---------------------------------------------------------------------------

class Bulle:
    def __init__(self, app):
        self.app = app
        self.s = s = user32.GetDpiForSystem() / 96      # facteur d'échelle (écrans 125 %, 150 %...)
        self.l, self.h = int(640 * s), int(270 * s)

        # Position : centrée en bas de l'écran principal, juste au-dessus de la barre des tâches
        zone = wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(zone), 0)
        self.x = (zone.left + zone.right - self.l) // 2
        self.y = zone.bottom - self.h - int(8 * s)

        # Fenêtre Windows native (tkinter ne permet pas la vraie transparence pixel par pixel)
        self.procedure = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        classe = WNDCLASSW(lpfnWndProc=self.procedure, hInstance=kernel32.GetModuleHandleW(None),
                           lpszClassName="ParleBulle")
        user32.RegisterClassW(ctypes.byref(classe))
        styles = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(styles, "ParleBulle", "Parle", WS_POPUP, self.x, self.y,
                                           self.l, self.h, None, None, classe.hInstance, None)
        # La bulle est invisible pour les captures d'écran : on peut ainsi photographier
        # ce qu'il y a derrière elle pour le flouter (effet verre).
        user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)

        self.police = police(int(15 * s))
        self.interligne = int(21 * s)
        self.visible = False
        self.opacite = 0.0
        self.etat = "ecoute"
        self.fond = None
        self.capture = 0.0
        self.lisse = 0.0
        self.barres = [0.0] * 7
        self.phase = 0
        self.cache_lignes = ("", [])

    def capturer(self):
        """Photographie l'écran derrière la bulle, puis le floute et ravive les couleurs."""
        img = ImageGrab.grab((self.x, self.y, self.x + self.l, self.y + self.h))
        img = img.filter(ImageFilter.GaussianBlur(22 * self.s))
        self.fond = ImageEnhance.Color(img).enhance(1.7).convert("RGBA")
        self.capture = time.time()

    def tick(self):
        """Appelé environ 60 fois par seconde : gère le fondu et redessine la bulle."""
        etat = self.app.bulle
        if etat:
            self.etat = etat
        cible = 1.0 if etat else 0.0
        self.opacite += (cible - self.opacite) * 0.3
        if abs(cible - self.opacite) < 0.03:
            self.opacite = cible

        if self.opacite > 0:
            if not self.visible or time.time() - self.capture > 0.25:
                self.capturer()
            poser(self.hwnd, self.dessiner(self.etat), self.x, self.y, self.opacite)
            if not self.visible:
                # Affiche la fenêtre au premier plan sans lui donner le focus
                user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x40)
                self.visible = True
        elif self.visible:
            user32.ShowWindow(self.hwnd, 0)
            self.visible = False

    def boucle(self):
        """Boucle principale de l'appli : traite les messages Windows et anime la bulle."""
        message = wintypes.MSG()
        while self.app.vivant:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            self.tick()
            time.sleep(0.016 if self.visible else 0.033)
        user32.DestroyWindow(self.hwnd)

    def verre(self, cadre, x, y, l, h, r):
        """Dessine un panneau en verre dépoli (ombre, fond flouté, teinte, reflet, contour)."""
        s = self.s
        calque, marge = ombre(l, h, r, int(12 * s))
        cadre.alpha_composite(calque, (x - marge, y - marge + int(6 * s)))

        verre = self.fond.crop((x, y, x + l, y + h))
        # Plus le fond est clair, plus on assombrit le verre pour que le texte blanc reste lisible
        lum = np.asarray(verre.convert("L"), dtype=np.float32).mean() / 255
        teinte = int(255 * (0.22 + 0.4 * max(0.0, lum - 0.45) / 0.55))
        verre.alpha_composite(Image.new("RGBA", (l, h), (16, 16, 20, teinte)))

        reflet = Image.new("RGBA", (l, h), (255, 255, 255, 255))
        reflet.putalpha(degrade(l, h, 34, 0))
        verre.alpha_composite(reflet)
        verre.putalpha(masque(l, h, r))
        cadre.alpha_composite(verre, (x, y))

        bord = Image.new("RGBA", (l, h), (255, 255, 255, 255))
        bord.putalpha(liseret(l, h, r, 1.2 * s))
        cadre.alpha_composite(bord, (x, y))

    def lignes(self, texte):
        """Coupe le texte en lignes et ne garde que les 3 dernières."""
        if self.cache_lignes[0] == texte:
            return self.cache_lignes[1]
        largeur, lignes, ligne = 500 * self.s, [], ""
        for mot in texte.split():
            essai = joindre(ligne, mot)
            if ligne and self.police.getlength(essai) > largeur:
                lignes.append(ligne)
                ligne = mot
            else:
                ligne = essai
        if ligne:
            lignes.append(ligne)
        if len(lignes) > 3:
            lignes = lignes[-3:]
            lignes[0] = "…" + lignes[0]
        self.cache_lignes = (texte, lignes)
        return lignes

    def dessiner(self, etat):
        """Dessine une image complète de la bulle pour l'état donné."""
        s, app = self.s, self.app
        self.phase += 1
        cadre = Image.new("RGBA", (self.l, self.h), (0, 0, 0, 0))
        teinte = TEINTES[etat]

        # Pilule du bas
        pl, ph = int(184 * s), int(52 * s)
        px, py = (self.l - pl) // 2, self.h - ph - int(22 * s)
        self.verre(cadre, px, py, pl, ph, ph // 2)

        # Point lumineux : grossit avec la voix pendant l'écoute, pulse pendant la transcription
        niveau = app.niveau if etat == "ecoute" else 0.0
        self.lisse += (niveau - self.lisse) * 0.35
        cx, cy = px + int(28 * s), py + ph // 2
        if etat == "ecoute":
            r = 5.5 * s * (1 + 0.45 * self.lisse)
        elif etat == "transcription":
            r = 5.5 * s * (1 + 0.15 * math.sin(self.phase / 4))
        else:
            r = 6 * s
        lueur = halo(int(6 * s), teinte)
        cadre.alpha_composite(lueur, (cx - lueur.width // 2, cy - lueur.height // 2))
        d = ImageDraw.Draw(cadre)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=teinte + (255,))

        # Onde de 7 barres arrondies
        k = 3
        zone_l, zone_h = int(108 * s), int(34 * s)
        zone = Image.new("RGBA", (zone_l * k, zone_h * k), (0, 0, 0, 0))
        dz = ImageDraw.Draw(zone)
        for i in range(7):
            if etat == "ecoute":
                cible = self.lisse * PROFIL[i] * (0.65 + 0.35 * math.sin(self.phase / 3.5 + i * 1.3))
            elif etat == "transcription":
                cible = 0.18 + 0.22 * (0.5 + 0.5 * math.sin(self.phase / 4 - i * 0.8))
            else:
                cible = 0.0
            self.barres[i] += (cible - self.barres[i]) * 0.4    # mouvement adouci
            hb = max(5 * s, self.barres[i] * 30 * s) * k
            lb = 5 * s * k
            bx = (i * 15 * s + 3 * s) * k
            if etat == "fini":
                couleur = (200, 255, 214, 230)
            elif etat == "erreur":
                couleur = (255, 160, 150, 230)
            else:
                couleur = (255, 255, 255, 240 if etat != "ecoute" or self.lisse > 0.3 else 140)
            dz.rounded_rectangle((bx, zone_h * k / 2 - hb / 2, bx + lb, zone_h * k / 2 + hb / 2), radius=lb / 2, fill=couleur)
        zone = zone.resize((zone_l, zone_h), Image.LANCZOS)
        cadre.alpha_composite(zone, (px + int(52 * s), cy - zone_h // 2))

        # Carte de texte au-dessus de la pilule
        texte, couleur = app.texte, (255, 255, 255, 250)
        if not texte and etat == "ecoute":
            texte, couleur = "Je t'écoute…", (255, 255, 255, 150)
        if texte:
            lignes = self.lignes(texte)
            largeur = int(max(self.police.getlength(l) for l in lignes))
            cl = largeur + int(40 * s)
            ch = len(lignes) * self.interligne + int(24 * s)
            cx0, cy0 = (self.l - cl) // 2, py - int(12 * s) - ch
            self.verre(cadre, cx0, cy0, cl, ch, int(22 * s))
            d = ImageDraw.Draw(cadre)
            for n, ligne in enumerate(lignes):
                lx = self.l / 2 - self.police.getlength(ligne) / 2
                ly = cy0 + int(11 * s) + n * self.interligne
                d.text((lx, ly + 1), ligne, font=self.police, fill=(0, 0, 0, 70))
                d.text((lx, ly), ligne, font=self.police, fill=couleur)
        return cadre


# ---------------------------------------------------------------------------
# L'application : raccourci clavier, micro, transcription, icône
# ---------------------------------------------------------------------------

class Parle:
    def __init__(self):
        self.config = lire_config()
        self.actif = self.config["actif"]
        self.micro = self.config["micro"]
        self.vivant = True

        # Ce que la bulle doit afficher
        self.bulle = None           # None (cachée), "ecoute", "transcription", "fini" ou "erreur"
        self.texte = ""
        self.niveau = 0.0           # volume de la voix, entre 0 et 1

        # État de la dictée en cours
        self.enregistre = False
        self.tenu = False           # Ctrl + Espace est maintenu
        self.occupe = False
        self.numero = 0
        self.morceaux = []          # morceaux de son reçus du micro
        self.sr = FREQ              # fréquence du micro

        # Transcription au fil de l'eau : le début de la phrase est validé pendant qu'on parle
        self.base = 0               # position (en échantillons) où commence la partie non validée
        self.couvert = 0            # quantité de son déjà transcrite par l'aperçu
        self.valide = ""            # texte définitif
        self.provisoire = ""        # texte de la fin, qui peut encore changer

        # Le modèle Whisper est chargé à la demande puis libéré après un moment sans dictée
        self.modele = None
        self.verrou = threading.Lock()
        self.dernier = 0

        self.icone = pystray.Icon("parle", icone(ICONES["pret"]), "Parle", menu=pystray.Menu(
            pystray.MenuItem("Activée", self.basculer, checked=lambda i: self.actif),
            pystray.MenuItem("Micro", pystray.Menu(self.menu_micros)),
            pystray.MenuItem("Lancer au démarrage", self.basculer_demarrage, checked=lambda i: demarrage_actif()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", self.quitter)))

    # --- Menu de l'icône -------------------------------------------------------

    def sauver(self):
        DOSSIER.mkdir(parents=True, exist_ok=True)
        self.config.update(actif=self.actif, micro=self.micro)
        CONFIG.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def menu_micros(self):
        items = [pystray.MenuItem("Par défaut (Windows)", self.choisir(None), checked=lambda i: self.micro is None, radio=True)]
        for nom in micros():
            items.append(pystray.MenuItem(nom, self.choisir(nom), checked=lambda i, n=nom: self.micro == n, radio=True))
        return items

    def choisir(self, nom):
        def action():
            self.micro = nom
            self.sauver()
        return action

    def basculer(self):
        self.actif = not self.actif
        self.sauver()
        self.maj_icone()

    def basculer_demarrage(self):
        regler_demarrage(not demarrage_actif())
        self.icone.update_menu()

    def maj_icone(self):
        if not self.actif:
            nom, titre = "off", "désactivée"
        elif self.enregistre:
            nom, titre = "ecoute", "écoute…"
        elif self.occupe:
            nom, titre = "transcription", "transcription…"
        else:
            nom, titre = "pret", "maintenir Ctrl + Espace"
        self.icone.icon = icone(ICONES[nom])
        self.icone.title = f"Parle : {titre}"

    def quitter(self):
        self.vivant = False
        keyboard.unhook_all()
        self.icone.stop()

    # --- Clavier ---------------------------------------------------------------

    def clavier(self, e):
        """Reçoit toutes les touches. Renvoyer False bloque la touche, True la laisse passer."""
        if e.scan_code != SCAN_ESPACE:
            return True
        if e.event_type == keyboard.KEY_DOWN:
            if self.tenu:
                return False    # bloque la répétition automatique d'Espace
            if self.actif and not self.occupe and keyboard.is_pressed(SCAN_CTRL):
                self.tenu = True
                self.enregistre = True
                self.occupe = True
                threading.Thread(target=self.session, daemon=True).start()
                return False
            return True
        if self.tenu:
            # Espace relâché : fin de l'enregistrement
            self.tenu = False
            self.enregistre = False
            return False
        return True

    # --- Modèle Whisper ----------------------------------------------------------

    def charger(self):
        with self.verrou:
            if self.modele is None:
                self.modele = WhisperModel(self.config["modele"], device="cpu", compute_type="int8",
                                           cpu_threads=min(8, os.cpu_count() or 4))
            self.dernier = time.time()

    def surveiller(self):
        """Libère la mémoire du modèle quand on ne dicte plus depuis un moment."""
        while self.vivant:
            time.sleep(5)
            with self.verrou:
                delai = self.config["garder_modele_secondes"]
                if self.modele is not None and not self.occupe and time.time() - self.dernier > delai:
                    self.modele = None
                    gc.collect()

    # --- Son -----------------------------------------------------------------------

    def enregistrer(self, indata, frames, temps, status):
        """Appelé par le micro à chaque petit morceau de son."""
        bloc = indata[:, 0].copy()
        self.morceaux.append(bloc)
        db = 20 * math.log10(float(np.sqrt(np.mean(bloc ** 2))) + 1e-7)
        self.niveau = min(1.0, max(0.0, (db + 55) / 45))

    def audio(self):
        """Assemble le son enregistré et le convertit en 16 kHz pour Whisper."""
        brut = np.concatenate(list(self.morceaux)) if self.morceaux else np.zeros(0, dtype=np.float32)
        if self.sr == FREQ:
            return brut
        if self.sr % FREQ == 0:
            k = self.sr // FREQ
            return brut[: len(brut) // k * k].reshape(-1, k).mean(axis=1).astype(np.float32)
        return np.interp(np.arange(0, len(brut), self.sr / FREQ), np.arange(len(brut)), brut).astype(np.float32)

    def ouvrir(self):
        """Ouvre et démarre le micro."""
        index, info = trouver_micro(self.micro)
        self.sr = int(info["default_samplerate"])
        options = {"extra_settings": sd.WasapiSettings(auto_convert=True)} if est_wasapi(info) else {}
        flux = sd.InputStream(device=index, samplerate=self.sr, channels=1, dtype="float32",
                              callback=self.enregistrer, **options)
        try:
            flux.start()
        except Exception:
            flux.close()
            raise
        return flux

    # --- Transcription ---------------------------------------------------------------

    def segments(self, audio, horodatage=True):
        """Transcrit un morceau de son. Renvoie une liste de (heure de fin, texte)."""
        # Le vocabulaire et la fin du texte déjà validé aident Whisper à rester cohérent
        contexte = joindre(self.config["vocabulaire"], self.valide[-200:])
        resultat, _ = self.modele.transcribe(audio, language=self.config["langue"], beam_size=1, vad_filter=True,
                                             initial_prompt=contexte or None, condition_on_previous_text=False,
                                             without_timestamps=not horodatage)
        return [(s.end, s.text.strip()) for s in resultat]

    def apercu(self, chargement):
        """Pendant qu'on parle : transcrit en continu pour afficher le texte en direct.
        Les phrases terminées depuis plus de 1,5 s sont validées et ne sont plus retranscrites,
        ce qui rend la fin beaucoup plus rapide."""
        chargement.join()
        while self.enregistre:
            audio = self.audio()
            if len(audio) - self.couvert < FREQ * 0.4:
                time.sleep(0.05)
                continue
            couvert = len(audio)
            queue = audio[self.base:]
            segs = self.segments(queue)
            duree = len(queue) / FREQ
            stables = [x for x in segs if x[0] < duree - 1.5] if duree > 5 else []
            if stables:
                self.valide = joindre(self.valide, *(t for _, t in stables))
                self.base += int(stables[-1][0] * FREQ)
                segs = segs[len(stables):]
            self.provisoire = joindre(*(t for _, t in segs))
            self.couvert = couvert
            if self.enregistre:
                self.texte = joindre(self.valide, self.provisoire)

    def coller(self, texte):
        """Colle le texte là où se trouve le curseur, puis remet l'ancien presse-papier."""
        try:
            ancien = pyperclip.paste()
        except pyperclip.PyperclipException:
            ancien = None
        pyperclip.copy(texte)
        if keyboard.is_pressed(SCAN_CTRL):
            keyboard.send(SCAN_V)       # Ctrl est encore enfoncé : V suffit
        else:
            keyboard.send("ctrl+v")
        if ancien is not None:
            threading.Timer(0.5, pyperclip.copy, (ancien,)).start()

    def finir(self, etat=None, texte=None, duree=1.6):
        """Termine la dictée et affiche éventuellement un dernier message avant de cacher la bulle."""
        numero = self.numero
        self.enregistre = False
        self.occupe = False
        self.maj_icone()
        if etat:
            if texte:
                self.texte = texte
            self.bulle = etat
            time.sleep(duree)
        if self.numero == numero:       # ne cache pas la bulle si une nouvelle dictée a commencé
            self.bulle = None

    def session(self):
        try:
            self.deroulement()
        except Exception:
            journaliser(*sys.exc_info())
            self.finir("erreur", "Erreur, détails dans erreurs.log", 2.5)

    def deroulement(self):
        """Une dictée complète : écoute, transcription, collage."""
        self.numero += 1
        self.texte = ""
        self.niveau = 0.0
        self.morceaux = []
        self.base = 0
        self.couvert = 0
        self.valide = ""
        self.provisoire = ""
        self.bulle = "ecoute"
        self.maj_icone()

        # Le modèle se charge pendant qu'on parle, pour ne pas faire attendre
        chargement = threading.Thread(target=self.charger, daemon=True)
        chargement.start()

        try:
            try:
                flux = self.ouvrir()
            except sd.PortAudioError:
                # Le micro a pu être débranché ou redémarré : on recharge la liste et on réessaie
                sd._terminate()
                sd._initialize()
                flux = self.ouvrir()
        except Exception:
            journaliser(*sys.exc_info())
            chargement.join()
            self.finir("erreur", "Micro introuvable ou indisponible", 2.5)
            return

        apercu = threading.Thread(target=self.apercu, args=(chargement,), daemon=True)
        try:
            apercu.start()
            while self.enregistre:
                time.sleep(0.01)
        finally:
            flux.close()

        self.niveau = 0.0
        audio = self.audio()
        if len(audio) < FREQ * 0.4:     # appui trop court : on ignore
            chargement.join()
            apercu.join()
            self.finir()
            return

        self.bulle = "transcription"
        self.maj_icone()
        chargement.join()
        apercu.join()

        # Si la fin n'a rien de nouveau (silence), on reprend directement le texte de l'aperçu.
        # Sinon on ne transcrit que la partie pas encore validée.
        reste = audio[self.couvert:]
        if self.couvert and (len(reste) < FREQ * 0.1 or float(np.sqrt(np.mean(reste ** 2))) < 0.008):
            texte = joindre(self.valide, self.provisoire)
        else:
            texte = joindre(self.valide, *(t for _, t in self.segments(audio[self.base:], horodatage=False)))
        self.dernier = time.time()

        if not texte:
            self.finir("erreur", "Rien entendu", 1.2)
            return
        self.coller(texte + " ")
        self.finir("fini", texte)

    def lancer(self):
        if not CONFIG.exists():
            self.sauver()
        self.maj_icone()
        keyboard.hook(self.clavier, suppress=True)
        threading.Thread(target=self.surveiller, daemon=True).start()
        threading.Thread(target=self.icone.run, daemon=True).start()
        Bulle(self).boucle()


if __name__ == "__main__":
    # Empêche de lancer l'appli deux fois
    kernel32.CreateMutexW(None, False, "ParleWhisperLocale")
    if kernel32.GetLastError() == 183:
        sys.exit()
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    Parle().lancer()
