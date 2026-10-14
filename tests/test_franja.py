"""La franja horaria: trabajar solo entre dos horas del día (por ejemplo, de noche)."""

from datetime import datetime, time, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_batch_monitor.franja import (
    en_franja,
    franja_valida,
    leer_franja,
    proximo_inicio,
    segundos_hasta_inicio,
    texto_de_franja,
)


def a(h, m=0, s=0, dia=13):
    return datetime(2026, 10, dia, h, m, s)


class TestLeer:
    @pytest.mark.parametrize("texto, esperado", [
        ("22:00-07:00", (time(22, 0), time(7, 0))),
        (" 7:30 - 21:05 ", (time(7, 30), time(21, 5))),
        ("00:00-23:59", (time(0, 0), time(23, 59))),
        ("22:00–07:00", (time(22, 0), time(7, 0))),     # raya corta
        ("22:00—07:00", (time(22, 0), time(7, 0))),     # raya larga
        ("22:00 a 07:00", (time(22, 0), time(7, 0))),
    ])
    def test_formas_de_escribirla(self, texto, esperado):
        assert leer_franja(texto) == esperado

    @pytest.mark.parametrize("texto", ["", "   ", None])
    def test_vacia_es_sin_franja(self, texto):
        assert leer_franja(texto) is None
        assert franja_valida(texto)

    @pytest.mark.parametrize("texto", [
        "de noche", "22:00", "22-07", "22:00-", "-07:00", "25:00-07:00", "22:60-07:00", "22:00-24:00",
        "22:00-22:00", "22:00:00-07:00:00", "22.00-07.00", "10:00-11:00-12:00", "a-b",
    ])
    def test_lo_que_no_vale_se_rechaza_con_un_mensaje_que_dice_que_hacer(self, texto):
        assert not franja_valida(texto)
        with pytest.raises(ValueError) as e:
            leer_franja(texto)
        assert "franja" in str(e.value).lower()

    def test_el_texto_vuelve_normalizado(self):
        assert texto_de_franja(leer_franja("7:05 a 22:00")) == "07:05-22:00"


class TestDentroOFuera:
    NOCHE = (time(22, 0), time(7, 0))
    DIA = (time(9, 0), time(17, 30))

    @pytest.mark.parametrize("hora, dentro", [
        (a(21, 59, 59), False), (a(22, 0), True), (a(23, 59, 59), True), (a(0, 0), True), (a(3, 15), True),
        (a(6, 59, 59), True), (a(7, 0), False), (a(12, 0), False),
    ])
    def test_una_franja_que_cruza_la_medianoche(self, hora, dentro):
        assert en_franja(self.NOCHE, hora) is dentro

    @pytest.mark.parametrize("hora, dentro", [
        (a(8, 59), False), (a(9, 0), True), (a(17, 29, 59), True), (a(17, 30), False), (a(23, 0), False), (a(0, 0), False),
    ])
    def test_una_franja_dentro_del_mismo_dia(self, hora, dentro):
        assert en_franja(self.DIA, hora) is dentro

    def test_el_inicio_cuenta_y_el_fin_no(self):
        assert en_franja(self.DIA, a(9, 0)) and not en_franja(self.DIA, a(17, 30))

    def test_los_segundos_no_mueven_el_limite(self):
        assert en_franja(self.DIA, a(9, 0, 0)) and en_franja(self.DIA, a(9, 0, 59))
        assert not en_franja(self.DIA, a(8, 59, 59))


class TestCuandoEmpieza:
    NOCHE = (time(22, 0), time(7, 0))

    def test_dentro_no_hay_que_esperar(self):
        assert segundos_hasta_inicio(self.NOCHE, a(23, 0)) == 0.0

    def test_fuera_cuenta_hasta_el_inicio_de_hoy(self):
        assert segundos_hasta_inicio(self.NOCHE, a(12, 0)) == 10 * 3600
        assert proximo_inicio(self.NOCHE, a(12, 0)) == a(22, 0)

    def test_si_el_inicio_de_hoy_ya_paso_es_el_de_manana(self):
        dia = (time(9, 0), time(17, 0))
        assert proximo_inicio(dia, a(18, 0)) == a(9, 0, dia=14)
        assert segundos_hasta_inicio(dia, a(18, 0)) == 15 * 3600

    def test_pasada_la_medianoche_dentro_de_la_noche_ya_no_espera(self):
        assert segundos_hasta_inicio(self.NOCHE, a(0, 30)) == 0.0

    def test_conserva_la_zona_horaria(self):
        madrid = timezone(timedelta(hours=2))
        ahora = datetime(2026, 10, 13, 12, 0, tzinfo=madrid)
        inicio = proximo_inicio(self.NOCHE, ahora)
        assert inicio.tzinfo is madrid and inicio == datetime(2026, 10, 13, 22, 0, tzinfo=madrid)


HORAS = st.times().map(lambda t: t.replace(second=0, microsecond=0))
INSTANTES = st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2028, 1, 1))


@given(inicio=HORAS, fin=HORAS, ahora=INSTANTES)
def test_propiedades_de_cualquier_franja(inicio, fin, ahora):
    if inicio == fin:
        return
    franja = (inicio, fin)
    espera = segundos_hasta_inicio(franja, ahora)
    assert 0.0 <= espera <= 24 * 3600
    assert (espera == 0.0) == en_franja(franja, ahora)
    if espera > 0:
        llegada = ahora + timedelta(seconds=espera)
        assert en_franja(franja, llegada)                                   # al llegar, ya está dentro…
        assert not en_franja(franja, llegada - timedelta(minutes=1))        # …y un minuto antes no
        assert proximo_inicio(franja, ahora) == llegada


@given(inicio=HORAS, fin=HORAS)
def test_toda_hora_cae_en_una_franja_o_en_su_complementaria(inicio, fin):
    if inicio == fin:
        return
    ahora = datetime(2026, 10, 13, 12, 34)
    assert en_franja((inicio, fin), ahora) != en_franja((fin, inicio), ahora)
