from collections import defaultdict, OrderedDict
import json
import tempfile

from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from . import models, runner, tasks
from ..libraries.models import Library
from ..repos.tasks import pull
from ..repos.utils import download_repository
from ..services.models import Service
from .utils import create_git_issue, PatchHandler


class AuditOverview(TemplateView):
    template_name = "audit_overview.html"

    @staticmethod
    def get_available_namespaces():
        return {i.kind.namespace for i in models.Issue.objects.all()}

    @staticmethod
    def get_available_owners():
        return {s.owner for s in Service.objects.all()}

    @staticmethod
    def get_available_status():
        return {s.status for s in Service.objects.all() if s.status is not None}

    @classmethod
    def get_available_filters(cls):
        return [
            *({"name": s, "type": "owner"} for s in cls.get_available_owners()),
            *({"name": n, "type": "namespace"} for n in cls.get_available_namespaces()),
            *({"name": n, "type": "status"} for n in cls.get_available_status()),
        ]

    @staticmethod
    def get_services(owner_filters, status_filters):
        service_list = (
            Service.objects.exclude(repository_id=None)
            .select_related("repository")
            .prefetch_related("repository__issues")
        )

        if owner_filters:
            service_list = service_list.filter(owner__in=owner_filters)

        if status_filters:
            service_list = service_list.filter(status__in=status_filters)

        return service_list

    @classmethod
    def get_issues(cls, owner_filters, namespace_filters, status_filters):
        kinds = defaultdict(lambda: {"services": []})

        if not namespace_filters:
            namespace_filters = cls.get_available_namespaces()

        for service in cls.get_services(owner_filters, status_filters):
            for issue in service.repository.issues.all():
                if issue.kind.namespace not in namespace_filters:
                    continue

                kinds[issue.kind_key]["title"] = issue.kind.title
                kinds[issue.kind_key]["description"] = issue.kind.description
                kinds[issue.kind_key]["effort"] = issue.kind.effort.value
                kinds[issue.kind_key]["severity"] = issue.kind.severity.value
                kinds[issue.kind_key]["services"].append(
                    {
                        "id": service.id,
                        "pk": issue.pk,
                        "url": issue.remote_issue_url,
                        "status": issue.status,
                        "kind_key": issue.kind_key,
                        "remote_id": issue.remote_issue_id
                        if issue.remote_issue_id is not None
                        else None,
                    }
                )

        return OrderedDict(sorted(kinds.items()))

    def get(self, request, *args, **kwargs):
        if request.is_ajax():
            owner_filters = self.request.GET.getlist("service_owner")
            namespace_filters = self.request.GET.getlist("namespace")
            status_filters = self.request.GET.getlist("status")

            return JsonResponse(
                {
                    "services": [
                        {"id": s.id, "name": s.name, "owner": s.owner}
                        for s in self.get_services(owner_filters, status_filters)
                    ],
                    "issues": self.get_issues(
                        owner_filters, namespace_filters, status_filters
                    ),
                    "filters": self.get_available_filters(),
                    "applied_filters": [
                        *({"name": s, "type": "owner"} for s in owner_filters),
                        *({"name": n, "type": "namespace"} for n in namespace_filters),
                        *({"name": n, "type": "status"} for n in status_filters),
                    ],
                }
            )

        return self.render_to_response(self.get_context_data(**kwargs))


class AuditReport(TemplateView):
    template_name = "audit_report.html"
    models = {"services": Service, "libraries": Library}

    def get(self, request, *args, **kwargs):
        context = self.get_context_data()
        project = context["project"]

        if "force" in self.request.GET and project.repository:
            pull(project.repository.remote_id, project.repository.provider)
            return redirect(
                "audit_report", self.kwargs["owner_slug"], self.kwargs["name_slug"]
            )

        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project_cls = self.models.get(self.kwargs["project_type"])

        if project_cls is None:
            raise Http404("Project.DoesNotExist")

        try:
            project = (
                project_cls.objects.select_related("repository")
                .prefetch_related("repository__issues")
                .get(
                    owner_slug=self.kwargs["owner_slug"],
                    name_slug=self.kwargs["name_slug"],
                )
            )
        except project_cls.DoesNotExist:
            raise Http404(f"{project_cls.__name__}.DoesNotExist")

        context["project"] = project
        context["project_type"] = self.kwargs["project_type"]
        context["issues"] = defaultdict(list)

        if project.repository:
            for issue in project.repository.issues.filter(deleted=False).exclude(
                status__in=[
                    models.Issue.Status.FIXED.value,
                    models.Issue.Status.NOT_FOUND.value,
                    models.Issue.Status.WONTFIX.value,
                ]
            ):
                context["issues"][issue.kind.category].append(issue)

        # with defaultdict {{ issues.items }} would be empty, and we want consistent issue order anyway
        context["issues"] = OrderedDict(
            (key, sorted(value, key=lambda x: x.kind_key))
            for key, value in sorted(context["issues"].items())
        )

        deleted_issues = project.repository.issues.filter(deleted=True).all()
        if deleted_issues:
            unknown_ctg = "Deprecated Issues"
            context["issues"][unknown_ctg] = deleted_issues

        return context


class IssuePatch(TemplateView):
    template_name = "issue_patch.html"

    def get(self, request, *args, **kwargs):
        issue = models.Issue.objects.get(pk=self.kwargs["issue_pk"])
        repository = issue.repository

        with tempfile.TemporaryDirectory() as repo_dir:
            repo_path = download_repository(repository, repo_dir)
            context = runner.CheckContext(issue.repository, repo_path)

            handler = PatchHandler(issue)
            patches = handler.run_patches(context)

        patches_key = handler.save_patches()
        context = {"patches": patches, "patches_key": patches_key}
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        issue = models.Issue.objects.get(pk=self.kwargs["issue_pk"])
        handler = PatchHandler(issue)

        if not handler.handle_patches(request):
            return HttpResponseForbidden()

        return redirect(
            "audit_report",
            self.kwargs["project_type"],
            self.kwargs["owner_slug"],
            self.kwargs["name_slug"],
        )


@require_POST
def open_bulk_git_issues(request):
    data = json.loads(request.body)

    user_name = request.user.get_username()
    redirect_uri = request.build_absolute_uri(
        reverse("audit_overview")
        if "owner" not in data
        else reverse("owned_audit_overview", args=[data["owner"]])
    )
    issues = [(pk, user_name, redirect_uri) for pk in data["pk_list"]]
    tasks.bulk_create_git_issues.delay(issues)

    owner_filters = {
        f["name"] for f in data["filters"]["applied"] if f["type"] == "owner"
    }
    namespace_filters = {
        f["name"] for f in data["filters"]["applied"] if f["type"] == "namespace"
    }
    status_filters = {
        f["name"] for f in data["filters"]["applied"] if f["type"] == "status"
    }

    return JsonResponse(
        {
            "services": [
                {"id": s.id, "name": s.name, "owner": s.owner}
                for s in AuditOverview.get_services(owner_filters, status_filters)
            ],
            "issues": AuditOverview.get_issues(
                owner_filters, namespace_filters, status_filters
            ),
            "filters": data["filters"],
        }
    )


@require_POST
def open_git_issue(request, project_type, owner_slug, name_slug, issue_pk):
    issue = models.Issue.objects.get(pk=issue_pk)

    create_git_issue(
        issue,
        request.user.get_username(),
        request.build_absolute_uri(
            reverse("audit_report", args=[project_type, owner_slug, name_slug])
        ),
    )

    return redirect("audit_report", project_type, owner_slug, name_slug)


@require_POST
def wontfix_issue(request, project_type, owner_slug, name_slug, issue_pk):
    issue = models.Issue.objects.get(pk=issue_pk)

    issue.status = issue.Status.WONTFIX.value
    issue.comment = request.POST["comment"]
    issue.full_clean()
    issue.save()

    return redirect("audit_report", project_type, owner_slug, name_slug)
