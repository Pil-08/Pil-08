from decimal import Decimal as D

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.estimator import MAX_ORDENES_CONTEO_EXACTO, estimar


class ClienteConteo:
    """Simula messages.count_tokens: 100 tokens por llamada."""

    def __init__(self, falla=False):
        self.llamadas = 0
        self.falla = falla
        self.messages = self

    def count_tokens(self, **kwargs):
        self.llamadas += 1
        if self.falla:
            raise RuntimeError("sin red")

        class R:
            input_tokens = 100

        return R()


def _cfg(**kw):
    base = dict(modelo="claude-haiku-4-5", max_tokens=1000, tope_gasto_usd=D(100))
    base.update(kw)
    return Configuracion(**base)


class TestEstimacionLocal:
    def test_el_peor_caso_es_entrada_mas_max_tokens(self):
        e = estimar(_cfg(), ["hola"] * 2)  # haiku: 1 / 5
        # 2 x (tokens x $1/M + 1000 x $5/M)  ~ 2 x $0.005
        assert e.ordenes == 2 and not e.exacta
        assert D("0.0100") <= e.coste_maximo < D("0.0101")

    def test_modo_lote_cuesta_la_mitad(self):
        normal = estimar(_cfg(), ["hola"] * 4).coste_maximo
        lote = estimar(_cfg(modo="lote"), ["hola"] * 4)
        assert lote.coste_maximo == normal / 2 and "lote" in lote.modo

    def test_avisa_si_el_peor_caso_supera_el_tope(self):
        e = estimar(_cfg(max_tokens=64_000, tope_gasto_usd=D("0.1")), ["x"] * 10)
        assert any("supera tu tope" in a for a in e.avisos)

    def test_no_avisa_si_cabe(self):
        assert not estimar(_cfg(), ["x"]).avisos

    def test_avisa_si_el_prompt_de_sistema_es_demasiado_corto_para_cachear(self):
        e = estimar(_cfg(system_prompt="Sé breve."), ["x"])  # Haiku 4.5 pide >= 4096
        assert any("caché" in a for a in e.avisos)

    def test_avisa_de_precio_aproximado_en_modelo_desconocido(self):
        e = estimar(_cfg(modelo="modelo-raro"), ["x"])
        assert any("modelo-raro" in a for a in e.avisos)

    def test_el_resumen_se_puede_imprimir(self):
        texto = "\n".join(estimar(_cfg(), ["x"]).resumen_lineas())
        assert "Coste MÁXIMO" in texto and "Órdenes detectadas" in texto


class TestConteoExacto:
    def test_usa_count_tokens_con_pocas_ordenes(self):
        c = ClienteConteo()
        e = estimar(_cfg(), ["a", "b", "c"], client=c)
        assert e.exacta and e.tokens_entrada == 300 and c.llamadas == 3

    def test_con_muchas_ordenes_cae_a_la_estimacion_local(self):
        c = ClienteConteo()
        e = estimar(_cfg(), ["x"] * (MAX_ORDENES_CONTEO_EXACTO + 1), client=c)
        assert not e.exacta and c.llamadas == 0

    def test_si_la_api_falla_no_se_rompe(self):
        e = estimar(_cfg(), ["hola"], client=ClienteConteo(falla=True))
        assert e.ordenes == 1 and e.tokens_entrada > 0
