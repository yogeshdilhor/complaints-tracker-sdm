from django.urls import path

from . import views

app_name = "complaints"

urlpatterns = [
    path("", views.complaint_list, name="list"),
    path("c/<int:pk>/", views.complaint_detail, name="detail"),
    path("c/<int:pk>/do/<str:action>/", views.complaint_action, name="action"),
    path("c/<int:pk>/stage-from-document/", views.stage_from_document, name="stage_from_document"),
    path("documents/<int:pk>/", views.document_download, name="document"),
    path("documents/<int:pk>/summary/", views.document_summary, name="document_summary"),
    path("new/", views.intake_upload, name="intake_upload"),
    path("new/review/", views.intake_review, name="intake_review"),
    path("overdue/", views.overdue_today, name="overdue"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("export/", views.export_page, name="export"),
    path("export/download/", views.export_download, name="export_download"),
]
