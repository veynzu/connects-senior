#!/usr/bin/env python3
"""Ejecutor auditable y seguro de Connects Senior; no envía alertas ni usa credenciales de producción."""
import argparse, subprocess, sys, os, json, re
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent
QA=ROOT.parent/'docs'/'pruebas'
EVID=QA/'evidencias'/'AUTOMATIZADAS'
TEST=ROOT/'app'/'tests'

def clean(s):
    s=re.sub(r'(?i)(postgres(?:ql)?(?:\+psycopg)?://)[^\s@]+@',r'\1[REDACTADO]@',s)
    s=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9_\-.]+',r'\1[REDACTADO]',s)
    s=re.sub(r'(?i)((?:api[_-]?key|password|token_fcm|secret)\s*[:=]\s*)[A-Za-z0-9_\-.]+',r'\1[REDACTADO]',s)
    return s

p=argparse.ArgumentParser(description='Suite de calidad. SQL solo en Neon de pruebas por confirmación explícita.')
p.add_argument('--suite',choices=['sintaxis','ddl','unitarias','sql','frontend'],required=True)
p.add_argument('--confirmo-neon-pruebas',action='store_true')
a=p.parse_args()
EVID.mkdir(parents=True,exist_ok=True)
now=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
tag=f'EV-AUTO-{a.suite.upper()}-{now}'
log=EVID/(tag+'.log')
report=EVID/(tag+'.json')
xml=EVID/(tag+'.xml')
status='EJECUTADO'
if a.suite=='sql':
    # Este flag exige que el operador use exclusivamente una BD desechable o rama de Neon.
    if not a.confirmo_neon_pruebas or not os.getenv('CS_TEST_DATABASE_URL'):
        status='BLOQUEADO'; cmd=[]; output='Configura CS_TEST_DATABASE_URL para una rama aislada y usa --confirmo-neon-pruebas. No se abrió conexión.'; code=2
    else:
        # Los fixtures priorizan CS_TEST_DATABASE_URL presente en .env; impedir URL divergente.
        from dotenv import dotenv_values
        declared=dotenv_values(ROOT/'.env').get('CS_TEST_DATABASE_URL') if (ROOT/'.env').is_file() else None
        if declared and declared != os.environ['CS_TEST_DATABASE_URL']:
            status='BLOQUEADO';cmd=[];code=2
            output='CS_TEST_DATABASE_URL en .env difiere del entorno. Revisar la rama aislada; no se abrió conexión.'
        else:
            cmd=[sys.executable,'-m','pytest',*(str(p) for p in sorted(TEST.glob('test_sql_*.py'))),'-q','-rA','--tb=short','--junitxml='+str(xml)]
            os.environ['CS_SQL_TESTS']='1'
elif a.suite=='unitarias':
    cmd=[sys.executable,'-m','pytest',*(str(p) for p in sorted(TEST.glob('test_*.py')) if not p.name.startswith('test_sql_')),'-q','-rA','--tb=short','--junitxml='+str(xml)]
elif a.suite=='sintaxis':
    cmd=[sys.executable,'-m','compileall','-q','app']
elif a.suite=='ddl':
    cmd=[sys.executable,str(QA/'herramientas'/'verificar_ddl_v004.py')]
else:
    if not (ROOT.parent/'frontend-angular'/'node_modules').exists():
        status='BLOQUEADO';cmd=[];code=2;output='No existen node_modules. Ejecutar npm ci y repetir en Windows; no se ejecutó ng test.'
    else:
        cmd=['npm','test','--','--watch=false']
if status != 'BLOQUEADO':
    try:
        result=subprocess.run(cmd,cwd=ROOT.parent/'frontend-angular' if a.suite=='frontend' else ROOT,
            env=os.environ.copy(),capture_output=True,text=True,timeout=600)
        code=result.returncode
        output=clean(result.stdout+'\n'+result.stderr)
        if code==0: status='APROBADO'
        elif 'ModuleNotFoundError' in output or 'No module named' in output: status='BLOQUEADO_DEPENDENCIAS'
        else: status='NO_APROBADO'
    except (OSError,subprocess.TimeoutExpired) as ex:
        code=2;status='BLOQUEADO';output=f'No se pudo ejecutar: {type(ex).__name__}'
log.write_text(clean(output),encoding='utf8')
data={'id_evidencia':tag,'fecha_utc':datetime.now(timezone.utc).isoformat(),
      'suite':a.suite,'estado':status,'codigo_salida':code,'comando':(' '.join(cmd) if cmd else 'no ejecutado'),
      'archivo_log':log.name,'junit':xml.name if xml.exists() else None,
      'advertencia':'Las pruebas SQL requieren base de pruebas aislada; no incluir secretos ni datos personales.'}
report.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf8')
print(json.dumps(data,indent=2,ensure_ascii=False))
sys.exit(code)
