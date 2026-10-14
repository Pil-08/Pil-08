"""Textos del modo simple en español e inglés.

Solo el modo simple está traducido: es lo que ve cualquier persona al abrir el
programa. El modo avanzado y la terminal siguen en español.
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys

IDIOMAS = ("es", "en")
_idioma_actual = "es"

TEXTOS: dict[str, dict[str, str]] = {
    "es": {
        "app_title": "Claude · Cola de preguntas",
        "tagline": "Escribe tus preguntas. Se guardan y se envían solas; si hay que esperar, esperan.",
        "ask": "Escribe tu pregunta",
        "placeholder": "Por ejemplo: Explícame en tres frases qué es un token.",
        "send": "Enviar",
        "pause": "Pausar",
        "resume": "Reanudar",
        "hint_keys": "Enter envía · Mayús+Enter añade una línea",
        "hint_saved": "Todo se guarda. Si cierras el programa, lo pendiente sigue cuando lo abras.",
        "queue": "Cola",
        "col_q": "Pregunta",
        "col_s": "Estado",
        "answer": "Respuesta",
        "answer_empty": "Selecciona una pregunta de la cola para ver su respuesta.",
        "answer_waiting": "Esperando la respuesta…",
        "copy": "Copiar",
        "format": "Con formato",
        "copied": "Copiado.",
        "save_all": "Guardar todo…",
        "new_queue": "Nueva cola",
        "options": "Opciones",
        "st_pending": "En cola",
        "st_sending": "Enviando…",
        "st_waiting": "Esperando…",
        "st_done": "Lista",
        "st_refused": "Rechazada",
        "st_failed": "Error",
        "st_skipped": "Quitada",
        "status_idle": "Listo",
        "status_idle_n": "Listo · {n} en cola",
        "status_sending": "Enviando la pregunta {n}…",
        "status_wait_rate_limit": "La IA ha llegado a su límite por ahora. Sigo sola en {t}.",
        "status_wait_server": "La IA está saturada. Reintento en {t}.",
        "status_wait_sin_conexion": "Sin conexión a internet. Reintento en {t}.",
        "status_wait_cuota": "Voy un poco más despacio para no pasar el límite. Sigo en {t}.",
        "status_paused": "En pausa. Pulsa «Reanudar» para continuar.",
        "status_paused_n": "En pausa · {n} en cola. Pulsa «Reanudar» para continuar.",
        "status_stopped": "Detenida.",
        "spend": "Gastado {gastado} de {tope}",
        "spend_unlimited": "Gastado {gastado} (sin tope)",
        "menu_copy_answer": "Copiar respuesta",
        "menu_copy_question": "Copiar pregunta",
        "menu_ask_again": "Volver a preguntar",
        "menu_remove": "Quitar de la cola",
        "banner_budget": "Has llegado al tope de gasto ({gastado} de {tope}). La cola se ha detenido; "
                         "lo ya respondido está guardado.",
        "banner_no_credit": "Tu cuenta de Anthropic se ha quedado sin saldo. Añade crédito y pulsa «Reanudar».",
        "banner_account_limit": "Tu cuenta de Anthropic ha alcanzado su límite de gasto y no acepta más "
                                "preguntas por ahora.",
        "banner_auth": "La clave de API no es válida o ha caducado. Cámbiala en Opciones.",
        "banner_model": "El modelo elegido ya no está disponible. Elige otro en Opciones.",
        "banner_network": "Se cortó la conexión con Anthropic. Cuando vuelva internet, pulsa «Reanudar».",
        "banner_fatal": "La cola se ha detenido por un error.",
        "banner_detail": "Detalle: {texto}",
        "btn_raise_cap": "Subir el tope…",
        "btn_open_console": "Abrir la consola de Anthropic",
        "btn_change_key": "Cambiar la clave…",
        "btn_choose_model": "Elegir modelo…",
        "btn_retry": "Reanudar",
        # primera vez
        "welcome_title": "Primeros pasos",
        "welcome_text": "Esta aplicación envía tus preguntas a Claude y las guarda en una cola: si hay que "
                        "esperar (límite de uso, sin conexión…), espera sola y sigue. Elige cómo quieres usar Claude:",
        "welcome_step1": "1. Crea una cuenta en la consola de Anthropic.",
        "welcome_step2": "2. Añade un poco de saldo (con unos pocos dólares hay para cientos de preguntas).",
        "welcome_step3": "3. Crea una «API key», cópiala y pégala aquí.",
        "welcome_open_console": "Abrir la consola de Anthropic",
        "welcome_paste": "Pegar",
        "welcome_show": "Mostrar la clave",
        "welcome_test": "Probar y guardar",
        "welcome_privacy": "Pagas a Anthropic solo por lo que uses; esta aplicación no cobra nada. La clave "
                           "se guarda en el llavero de tu sistema y solo se envía a Anthropic.",
        "key_testing": "Probando la clave…",
        "key_ok": "Clave válida.",
        "key_saved_keyring": "Guardada en el llavero de tu sistema.",
        "key_saved_memory": "No pude guardarla de forma segura: tendrás que pegarla cada vez que abras el "
                            "programa (o definir la variable ANTHROPIC_API_KEY).",
        "key_empty": "Pega primero tu clave.",
        "key_bad": "Anthropic no reconoce esa clave. Comprueba que la copiaste entera.",
        "key_offline": "No hay conexión con Anthropic. Comprueba tu internet e inténtalo de nuevo.",
        "key_other": "No se pudo comprobar la clave: {texto}",
        # opciones
        "opt_title": "Opciones",
        "opt_key": "Clave de API",
        "opt_key_env": "Viene de la variable de entorno ANTHROPIC_API_KEY (edítala allí para cambiarla).",
        "opt_key_keyring": "Guardada en el llavero de tu sistema.",
        "opt_key_memory": "Solo en memoria (esta sesión).",
        "opt_key_none": "No hay clave.",
        "opt_model": "Modelo",
        "opt_load_models": "Cargar modelos",
        "opt_models_loaded": "{n} modelos cargados.",
        "opt_cap": "Tope de gasto (USD)",
        "opt_cap_help": "La cola se detiene al llegar a este gasto. 0 = sin tope (no recomendado).",
        "opt_autostart": "Continuar sola lo pendiente al abrir el programa",
        "opt_language": "Idioma (al reiniciar)",
        "opt_theme": "Tema (al reiniciar)",
        "theme_auto": "Automático",
        "theme_light": "Claro",
        "theme_dark": "Oscuro",
        "opt_instructions": "Instrucciones para todas las respuestas (opcional)",
        "opt_instructions_help": "Por ejemplo: «Responde siempre en español y con frases cortas». Se envían con cada pregunta.",
        "queue_empty": "Aquí irán apareciendo tus preguntas, de una en una.",
        "answer_placeholder": "Aquí verás la respuesta, escribiéndose en directo.",
        "unexpected_title": "Algo ha fallado",
        "unexpected_text": "Ha ocurrido un error inesperado dentro del programa. Lo que ya estaba guardado no se ha perdido.\n\n"
                           "{error}\n\nEl detalle está en el registro: {log}",
        "opt_advanced": "Modo avanzado…",
        "opt_folder": "Abrir carpeta de respuestas",
        "opt_save": "Guardar",
        "opt_cancel": "Cancelar",
        "opt_bad_cap": "El tope debe ser un número igual o mayor que 0.",
        "opt_about": "Versión {v}",
        # cierre
        "close_title": "Quedan preguntas en la cola",
        "close_text": "Hay {n} pregunta(s) sin responder. Se guardan y seguirán la próxima vez que abras el programa.",
        "close_quit": "Salir y guardar",
        "close_background": "Seguir en segundo plano",
        "close_cancel": "Volver",
        "new_confirm": "La cola actual se guarda en disco y empiezas una en blanco. ¿Continuar?",
        "new_blocked": "Hay preguntas en marcha. Pausa la cola antes de empezar otra.",
        "save_title": "Guardar las respuestas",
        "save_done": "Guardado en {ruta}",
        "save_error": "No se pudo guardar: {texto}",
        "save_empty": "Todavía no hay nada que guardar.",
        "resumed_found": "Tienes {n} pregunta(s) sin enviar de la última vez.",
        "tray_open": "Abrir",
        "tray_quit": "Salir",
        "no_key_title": "Falta la clave",
        # instalación de la librería que falta
        "install_title": "Falta una pieza",
        "install_text": "Para hablar con Claude, este programa necesita la librería oficial de Anthropic "
                        "(se llama «anthropic»). Se instala una sola vez, con este comando, y no se instala nada más:",
        "install_ask": "¿Instalarla ahora? [s/N] ",
        "install_button": "Instalar ahora",
        "install_quit": "Salir",
        "install_working": "Instalando… puede tardar un minuto.",
        "install_done": "Listo. Abriendo el programa…",
        "install_failed": "No se pudo instalar.",
        "install_copy": "Copiar comando",
        "install_retry": "Reintentar",
        "install_manual": "También puedes hacerlo tú en una terminal con el comando de arriba y volver a abrir el programa.",
        "install_loop": "La instalación terminó, pero Python sigue sin encontrar la librería «anthropic». "
                        "Instálala a mano con: {comando}",
        # error que nadie esperaba al abrir el programa
        "fatal_title": "No se pudo abrir el programa",
        "fatal_text": "Ha ocurrido un error inesperado y el programa se ha cerrado.",
        "fatal_log": "Los detalles están en este archivo (si pides ayuda, adjúntalo):\n{ruta}",
        # una segunda copia del programa
        "already_title": "Ya está abierto",
        "already_text": "Ya hay otra copia de Claude Batch Monitor en marcha{proceso}.\n\n"
                        "Dos copias a la vez se pisarían el gasto acumulado y el tope de gasto dejaría de proteger. "
                        "Búscala en la barra de tareas o en la bandeja del sistema, o ciérrala antes de abrir esta.",
        # plan de Claude (Pro/Max) a través de Claude Code
        "welcome_tab_plan": "Con mi plan de Claude",
        "welcome_tab_api": "Con una clave de API",
        "welcome_plan_text": "Usa el plan de Claude que ya pagas (Pro o Max): no pagas por cada pregunta. Cuando se "
                             "acaba tu cupo, el programa espera solo a que se renueve y sigue.",
        "welcome_plan_how": "Funciona a través de Claude Code, el programa oficial de Anthropic: tiene que estar "
                            "instalado y con tu sesión iniciada.",
        "plan_privacy": "Tus preguntas van a Claude a través de Claude Code, con tu cuenta. Este programa no ve ni "
                        "guarda tu contraseña ni tus credenciales.",
        "plan_checking": "Comprobando Claude Code…",
        "plan_ok": "Claude Code está listo y usa tu plan.",
        "plan_no_instalado": "No encuentro Claude Code en este ordenador. Instálalo, ábrelo una vez e inicia sesión con "
                             "tu cuenta; luego pulsa «Comprobar de nuevo».",
        "plan_antiguo": "Tu Claude Code es demasiado antiguo. Actualízalo (en una terminal: claude update) y vuelve "
                        "a comprobar.",
        "plan_sin_sesion": "Claude Code está instalado, pero no has iniciado sesión. En una terminal escribe "
                           "«claude auth login», elige tu cuenta de Claude y vuelve a comprobar.",
        "plan_no_es_plan": "Claude Code está iniciado con una clave de API (se paga por uso), no con tu plan. En una "
                           "terminal escribe «claude auth logout» y luego «claude auth login» con tu cuenta de Claude.",
        "plan_error": "No pude comprobar Claude Code: {texto}",
        "plan_use": "Usar mi plan",
        "plan_recheck": "Comprobar de nuevo",
        "plan_install": "Cómo instalar Claude Code",
        "plan_pick": "Elegir dónde está…",
        "plan_pick_title": "Elige el programa «claude» (Claude Code)",
        "opt_access": "Cómo usar Claude",
        "opt_access_plan": "Con mi plan de Claude (Pro o Max)",
        "opt_access_api": "Con una clave de API (se paga por uso)",
        "opt_plan_model": "Modelo (con el plan)",
        "opt_plan_model_default": "El de mi plan",
        "opt_plan_model_help": "Si no eliges ninguno, se usa el que tu plan trae por defecto.",
        "opt_window": "Trabajar solo en una franja horaria",
        "opt_window_from": "de",
        "opt_window_to": "a",
        "opt_window_help": "Por ejemplo, de 22:00 a 07:00: fuera de esas horas las preguntas esperan y no se envían.",
        "opt_bad_plan_model": "Ese nombre de modelo no vale. Usa un alias como sonnet u opus, o déjalo en «El de mi plan».",
        "opt_bad_window": "La franja debe ser de una hora a otra, como 22:00 a 07:00.",
        "status_wait_plan": "Has llegado al límite de tu plan. Sigo sola {cuando} (faltan {t}).",
        "status_wait_franja": "Fuera de tu franja horaria. Sigo sola {cuando} (faltan {t}).",
        "plan_usage": "Plan: {resumen}",
        "plan_included": "Incluido en tu plan",
        "btn_open_usage": "Ver mi uso",
        "banner_plan_extra": "Tu plan se agotó y Claude Code empezó a gastar créditos extra (de pago). La cola se "
                             "detuvo para no gastarlos; la última respuesta está guardada. Cuando tu plan se renueve, "
                             "pulsa «Reanudar».",
        "banner_plan_credits": "Tu plan se quedó sin uso incluido y Claude Code pide créditos de pago. Este programa no "
                               "los gasta. Cuando tu plan se renueve, pulsa «Reanudar».",
        "banner_plan_spend": "Has llegado a un límite de gasto de tu cuenta de Claude. Súbelo o espera a que se renueve "
                             "y pulsa «Reanudar».",
        "banner_plan_auth": "Claude Code no tiene la sesión iniciada o ha caducado. En una terminal escribe "
                            "«claude auth login» y pulsa «Reanudar».",
        "banner_plan_account": "Tu cuenta de Claude no puede usar Claude Code ahora mismo (en espera, suspendida o sin "
                               "permiso). Revisa tu cuenta.",
        "banner_plan_model": "Tu plan no permite el modelo elegido, o no existe. Elige otro en Opciones (o déjalo en "
                             "«El de mi plan»).",
        "banner_plan_apikey": "Claude Code está usando una clave de API (se paga por token), no tu plan. No se envió "
                              "nada. Quita la clave o inicia sesión con «claude auth login».",
        "banner_plan_nohour": "Claude Code dijo que se alcanzó el límite de tu plan pero no cuándo vuelve. Mira tu uso "
                              "y pulsa «Reanudar» cuando quieras reintentar.",
        "banner_plan_error": "No se pudo usar Claude Code.",
        "plan_win_session": "sesión",
        "plan_win_week": "semana",
        "plan_renews": "se renueva {cuando}",
        "when_today": "a las {hora}",
        "when_day": "el {dia} a las {hora}",
        "key_library": "Falta la librería «anthropic» para usar una clave de API. Instálala con:  {comando}  y vuelve a "
                       "abrir el programa. (Con tu plan de Claude no hace falta.)",
    },
    "en": {
        "app_title": "Claude · Question queue",
        "tagline": "Write your questions. They are saved and sent on their own; if something has to wait, they wait.",
        "ask": "Write your question",
        "placeholder": "For example: Explain in three sentences what a token is.",
        "send": "Send",
        "pause": "Pause",
        "resume": "Resume",
        "hint_keys": "Enter sends · Shift+Enter adds a line",
        "hint_saved": "Everything is saved. If you close the program, what is pending continues when you reopen it.",
        "queue": "Queue",
        "col_q": "Question",
        "col_s": "Status",
        "answer": "Answer",
        "answer_empty": "Select a question in the queue to see its answer.",
        "answer_waiting": "Waiting for the answer…",
        "copy": "Copy",
        "format": "Formatted",
        "copied": "Copied.",
        "save_all": "Save all…",
        "new_queue": "New queue",
        "options": "Options",
        "st_pending": "Queued",
        "st_sending": "Sending…",
        "st_waiting": "Waiting…",
        "st_done": "Done",
        "st_refused": "Declined",
        "st_failed": "Error",
        "st_skipped": "Removed",
        "status_idle": "Ready",
        "status_idle_n": "Ready · {n} queued",
        "status_sending": "Sending question {n}…",
        "status_wait_rate_limit": "The AI hit its usage limit for now. Resuming by itself in {t}.",
        "status_wait_server": "The AI is overloaded. Retrying in {t}.",
        "status_wait_sin_conexion": "No internet connection. Retrying in {t}.",
        "status_wait_cuota": "Slowing down a little to stay under the limit. Continuing in {t}.",
        "status_paused": "Paused. Press “Resume” to continue.",
        "status_paused_n": "Paused · {n} queued. Press “Resume” to continue.",
        "status_stopped": "Stopped.",
        "spend": "Spent {gastado} of {tope}",
        "spend_unlimited": "Spent {gastado} (no cap)",
        "menu_copy_answer": "Copy answer",
        "menu_copy_question": "Copy question",
        "menu_ask_again": "Ask again",
        "menu_remove": "Remove from queue",
        "banner_budget": "You reached your spending cap ({gastado} of {tope}). The queue stopped; "
                         "everything already answered is saved.",
        "banner_no_credit": "Your Anthropic account is out of credit. Add credit and press “Resume”.",
        "banner_account_limit": "Your Anthropic account reached its spending limit and is not accepting "
                                "questions for now.",
        "banner_auth": "The API key is invalid or expired. Change it in Options.",
        "banner_model": "The selected model is no longer available. Pick another in Options.",
        "banner_network": "The connection to Anthropic dropped. When the internet is back, press “Resume”.",
        "banner_fatal": "The queue stopped because of an error.",
        "banner_detail": "Detail: {texto}",
        "btn_raise_cap": "Raise the cap…",
        "btn_open_console": "Open the Anthropic console",
        "btn_change_key": "Change the key…",
        "btn_choose_model": "Choose a model…",
        "btn_retry": "Resume",
        "welcome_title": "Getting started",
        "welcome_text": "This app sends your questions to Claude and keeps them in a queue: if it has to wait "
                        "(usage limit, no connection…), it waits by itself and carries on. Choose how you want to use Claude:",
        "welcome_step1": "1. Create an account in the Anthropic console.",
        "welcome_step2": "2. Add a little credit (a few dollars cover hundreds of questions).",
        "welcome_step3": "3. Create an “API key”, copy it and paste it here.",
        "welcome_open_console": "Open the Anthropic console",
        "welcome_paste": "Paste",
        "welcome_show": "Show the key",
        "welcome_test": "Test and save",
        "welcome_privacy": "You pay Anthropic only for what you use; this app charges nothing. The key is "
                           "stored in your system keyring and is only ever sent to Anthropic.",
        "key_testing": "Testing the key…",
        "key_ok": "Valid key.",
        "key_saved_keyring": "Saved in your system keyring.",
        "key_saved_memory": "I couldn't store it securely: you will need to paste it every time you open the "
                            "program (or set the ANTHROPIC_API_KEY variable).",
        "key_empty": "Paste your key first.",
        "key_bad": "Anthropic doesn't recognise that key. Check that you copied all of it.",
        "key_offline": "Can't reach Anthropic. Check your internet and try again.",
        "key_other": "The key could not be checked: {texto}",
        "opt_title": "Options",
        "opt_key": "API key",
        "opt_key_env": "Comes from the ANTHROPIC_API_KEY environment variable (edit it there to change it).",
        "opt_key_keyring": "Stored in your system keyring.",
        "opt_key_memory": "Memory only (this session).",
        "opt_key_none": "No key.",
        "opt_model": "Model",
        "opt_load_models": "Load models",
        "opt_models_loaded": "{n} models loaded.",
        "opt_cap": "Spending cap (USD)",
        "opt_cap_help": "The queue stops when it reaches this spend. 0 = no cap (not recommended).",
        "opt_autostart": "Carry on with what's pending when the program opens",
        "opt_language": "Language (on restart)",
        "opt_theme": "Theme (on restart)",
        "theme_auto": "Automatic",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "opt_instructions": "Instructions for every answer (optional)",
        "opt_instructions_help": "For example: “Always answer in English, in short sentences.” They are sent with every question.",
        "queue_empty": "Your questions will appear here, one by one.",
        "answer_placeholder": "You will see the answer here, as it is written.",
        "unexpected_title": "Something went wrong",
        "unexpected_text": "An unexpected error happened inside the program. Nothing that was already saved has been lost.\n\n"
                           "{error}\n\nThe details are in the log: {log}",
        "opt_advanced": "Advanced mode…",
        "opt_folder": "Open the answers folder",
        "opt_save": "Save",
        "opt_cancel": "Cancel",
        "opt_bad_cap": "The cap must be a number equal to or greater than 0.",
        "opt_about": "Version {v}",
        "close_title": "Questions are still queued",
        "close_text": "{n} question(s) are unanswered. They are saved and will continue the next time you open the program.",
        "close_quit": "Quit and save",
        "close_background": "Keep running in the background",
        "close_cancel": "Go back",
        "new_confirm": "The current queue is kept on disk and you start a blank one. Continue?",
        "new_blocked": "Questions are in progress. Pause the queue before starting another.",
        "save_title": "Save the answers",
        "save_done": "Saved to {ruta}",
        "save_error": "Couldn't save: {texto}",
        "save_empty": "There is nothing to save yet.",
        "resumed_found": "You have {n} unsent question(s) from last time.",
        "tray_open": "Open",
        "tray_quit": "Quit",
        "no_key_title": "Missing key",
        # installing the missing library
        "install_title": "One piece is missing",
        "install_text": "To talk to Claude, this program needs Anthropic's official library (called “anthropic”). "
                        "It is installed once, with this command, and nothing else is installed:",
        "install_ask": "Install it now? [y/N] ",
        "install_button": "Install now",
        "install_quit": "Quit",
        "install_working": "Installing… this can take a minute.",
        "install_done": "Done. Opening the program…",
        "install_failed": "The installation failed.",
        "install_copy": "Copy command",
        "install_retry": "Try again",
        "install_manual": "You can also run the command above yourself in a terminal and open the program again.",
        "install_loop": "The installation finished, but Python still cannot find the “anthropic” library. "
                        "Install it by hand with: {comando}",
        # an error nobody expected while opening the program
        "fatal_title": "The program could not start",
        "fatal_text": "An unexpected error happened and the program has closed.",
        "fatal_log": "The details are in this file (attach it if you ask for help):\n{ruta}",
        # a second copy of the program
        "already_title": "Already open",
        "already_text": "Another copy of Claude Batch Monitor is already running{proceso}.\n\n"
                        "Two copies at once would overwrite each other's running spend total and the spend cap "
                        "would stop protecting you. Look for it in the taskbar or the system tray, or close it "
                        "before opening this one.",
        # Claude plan (Pro/Max) through Claude Code
        "welcome_tab_plan": "With my Claude plan",
        "welcome_tab_api": "With an API key",
        "welcome_plan_text": "Use the Claude plan you already pay for (Pro or Max): you don't pay per question. When "
                             "your allowance runs out, the program waits by itself until it renews and carries on.",
        "welcome_plan_how": "It works through Claude Code, Anthropic's official program: it has to be installed and "
                            "signed in.",
        "plan_privacy": "Your questions go to Claude through Claude Code, with your account. This program never sees "
                        "or stores your password or credentials.",
        "plan_checking": "Checking Claude Code…",
        "plan_ok": "Claude Code is ready and uses your plan.",
        "plan_no_instalado": "I can't find Claude Code on this computer. Install it, open it once and sign in with "
                             "your account; then press “Check again”.",
        "plan_antiguo": "Your Claude Code is too old. Update it (in a terminal: claude update) and check again.",
        "plan_sin_sesion": "Claude Code is installed but you are not signed in. In a terminal type “claude auth "
                           "login”, choose your Claude account and check again.",
        "plan_no_es_plan": "Claude Code is signed in with an API key (pay per use), not with your plan. In a terminal "
                           "type “claude auth logout” and then “claude auth login” with your Claude account.",
        "plan_error": "I couldn't check Claude Code: {texto}",
        "plan_use": "Use my plan",
        "plan_recheck": "Check again",
        "plan_install": "How to install Claude Code",
        "plan_pick": "Choose where it is…",
        "plan_pick_title": "Choose the “claude” program (Claude Code)",
        "opt_access": "How to use Claude",
        "opt_access_plan": "With my Claude plan (Pro or Max)",
        "opt_access_api": "With an API key (pay per use)",
        "opt_plan_model": "Model (with the plan)",
        "opt_plan_model_default": "My plan's default",
        "opt_plan_model_help": "If you pick none, your plan's default model is used.",
        "opt_window": "Work only during a time window",
        "opt_window_from": "from",
        "opt_window_to": "to",
        "opt_window_help": "For example, 22:00 to 07:00: outside those hours the questions wait and are not sent.",
        "opt_bad_plan_model": "That model name isn't valid. Use an alias such as sonnet or opus, or leave it on “My plan's default”.",
        "opt_bad_window": "The window must be from one time to another, like 22:00 to 07:00.",
        "status_wait_plan": "You reached your plan's limit. Resuming by itself {cuando} ({t} left).",
        "status_wait_franja": "Outside your time window. Resuming by itself {cuando} ({t} left).",
        "plan_usage": "Plan: {resumen}",
        "plan_included": "Included in your plan",
        "btn_open_usage": "See my usage",
        "banner_plan_extra": "Your plan ran out and Claude Code started spending extra credits (paid). The queue "
                             "stopped so it doesn't spend them; the last answer is saved. When your plan renews, "
                             "press “Resume”.",
        "banner_plan_credits": "Your plan has no included usage left and Claude Code asks for paid credits. This "
                               "program doesn't spend them. When your plan renews, press “Resume”.",
        "banner_plan_spend": "You reached a spending limit on your Claude account. Raise it or wait for it to renew "
                             "and press “Resume”.",
        "banner_plan_auth": "Claude Code is not signed in, or the session expired. In a terminal type “claude auth "
                            "login” and press “Resume”.",
        "banner_plan_account": "Your Claude account can't use Claude Code right now (on hold, suspended or without "
                               "permission). Check your account.",
        "banner_plan_model": "Your plan doesn't allow the chosen model, or it doesn't exist. Pick another in Options "
                             "(or leave it on “My plan's default”).",
        "banner_plan_apikey": "Claude Code is using an API key (pay per token), not your plan. Nothing was sent. "
                              "Remove the key or sign in with “claude auth login”.",
        "banner_plan_nohour": "Claude Code said your plan's limit was reached but not when it returns. Check your "
                              "usage and press “Resume” whenever you want to retry.",
        "banner_plan_error": "Claude Code could not be used.",
        "plan_win_session": "session",
        "plan_win_week": "week",
        "plan_renews": "renews {cuando}",
        "when_today": "at {hora}",
        "when_day": "on {dia} at {hora}",
        "key_library": "The “anthropic” library is missing, and it is needed to use an API key. Install it with:  "
                       "{comando}  and reopen the program. (With your Claude plan it isn't needed.)",
    },
}


def _idioma_de(codigo: str) -> str:
    """`es` o `en` si el código de idioma (es_ES.UTF-8, en-US, Spanish_Spain…) dice uno de los dos; si no, "".

    `C` y `POSIX` no dicen ningún idioma.
    """
    codigo = (codigo or "").strip().lower()
    for idioma, nombre in (("es", "spanish"), ("en", "english")):
        if re.match(rf"^({idioma}|{nombre})([_\-.@:]|$)", codigo):
            return idioma
    return ""


def _codigos_del_sistema() -> list[str]:
    """Los códigos de idioma que el sistema declara, del más al menos fiable."""
    codigos: list[str] = []
    try:
        if sys.platform == "win32":
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            codigos.append(locale.windows_locale.get(lcid, ""))
        elif sys.platform == "darwin":
            # Un programa abierto con doble clic no recibe LANG: el idioma está en los ajustes del sistema.
            r = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                               capture_output=True, text=True, timeout=3)
            lineas = [linea.strip(' ",()') for linea in r.stdout.splitlines()]  # ("es-ES", "en-US") -> es-ES, en-US
            codigos += [linea for linea in lineas if linea][:3]
    except Exception:
        pass
    codigos += [os.environ[v] for v in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE") if os.environ.get(v)]
    return codigos


def idioma_del_sistema() -> str:
    """`es` o `en` según el idioma del sistema; `es` si no se puede saber (o es otro idioma)."""
    for codigo in _codigos_del_sistema():
        idioma = _idioma_de(codigo)
        if idioma:
            return idioma
    return "es"


def fijar_idioma(codigo: str = "") -> str:
    """Fija el idioma del modo simple. '' = el del sistema. Devuelve el código usado."""
    global _idioma_actual
    codigo = (codigo or "").strip().lower()[:2]
    _idioma_actual = codigo if codigo in IDIOMAS else idioma_del_sistema()
    return _idioma_actual


def idioma() -> str:
    return _idioma_actual


def t(clave: str, **valores: object) -> str:
    """Texto traducido. Si falta la clave en el idioma actual cae al español, y si no, a la clave."""
    texto = TEXTOS.get(_idioma_actual, {}).get(clave) or TEXTOS["es"].get(clave) or clave
    if valores:
        try:
            return texto.format(**valores)
        except (KeyError, IndexError, ValueError):
            return texto
    return texto
