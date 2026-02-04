from auditlog.registry import auditlog
from .models import Training, Person, Participation, UofAssessment

auditlog.register(Training)
auditlog.register(Person)
auditlog.register(Participation)
auditlog.register(UofAssessment)
