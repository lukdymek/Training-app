from django.contrib.admin import AdminSite

class TrainingAdminSite(AdminSite):
    site_header = "Training App Admin"
    site_title = "Training App Admin"
    index_title = "Administration"
    site_url = "/calendar/"

    class Media:
        css = {
            "all": ("admin/custom.css",)
        }
