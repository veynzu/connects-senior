from enum import StrEnum


class Role(StrEnum):
    ADULTO_MAYOR = "adulto_mayor"
    FAMILIAR = "familiar"
    CUIDADOR = "cuidador"
    ADMINISTRADOR = "administrador"


class CallStatus(StrEnum):
    COMPLETADA = "completada"
    NO_CONTESTADA = "no_contestada"
    CANCELADA = "cancelada"


class AlertStatus(StrEnum):
    PENDIENTE = "pendiente"
    EN_ATENCION = "en_atencion"
    ATENDIDA = "atendida"


class AlertCategory(StrEnum):
    AYUDA_EN_CASA = "ayuda_en_casa"
    COMPANIA = "compania"
    PROBLEMA = "problema"
    OTRO = "otro"


class ReminderKind(StrEnum):
    CITA_MEDICA = "cita_medica"
    MEDICAMENTO = "medicamento"


class ReminderRepeat(StrEnum):
    NINGUNA = "ninguna"
    DIARIA = "diaria"
    SEMANAL = "semanal"


class ReminderStatus(StrEnum):
    PENDIENTE = "pendiente"
    CONFIRMADO = "confirmado"
    OMITIDO = "omitido"
