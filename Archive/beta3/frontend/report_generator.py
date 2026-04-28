import os
import html
import re
from datetime import datetime

class ReportGenerator:
    """Generates HTML reports from diagnostic results."""
    
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_html_report(self, cluster_name: str, results: dict):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = self._safe_filename_part(cluster_name)
        report_path = os.path.join(self.output_dir, f"report_{safe_name}_{timestamp}.html")
        escaped_cluster_name = html.escape(cluster_name)
        
        # In a full implementation, this would use a Jinja2 template
        # For now, we create a structured JSON-based HTML for the beta
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Diagnostic Report - {escaped_cluster_name}</title>
            <style>
                body {{ font-family: sans-serif; background: #0f172a; color: white; padding: 2rem; }}
                .card {{ background: #1e293b; border-radius: 0.5rem; padding: 1.5rem; margin-bottom: 1rem; }}
                .pass {{ color: #10b981; }}
                .fail {{ color: #ef4444; }}
                .warn {{ color: #f59e0b; }}
                .info, .skip {{ color: #cbd5e1; }}
                .error {{ color: #fb7185; }}
                .check-item {{ margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid rgba(255,255,255,0.08); }}
                .check-message {{ margin-bottom: 0.3rem; }}
                .check-detail {{ color: #cbd5e1; white-space: pre-wrap; font-size: 0.92rem; }}
            </style>
        </head>
        <body>
            <h1>CloudHealth Diagnostic Report</h1>
            <h2>Cluster: {escaped_cluster_name}</h2>
            <p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
            <div id="results">
                {self._format_results(results)}
            </div>
        </body>
        </html>
        """
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        return report_path

    def generate_combined_report(self, summaries: list[dict]):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(self.output_dir, f"healthcheck_report_{timestamp}.html")
        cards = []
        for summary in summaries:
            cluster_name = html.escape(summary.get("cluster_name", "Unknown Cluster"))
            status = html.escape(summary.get("overall_status", "INFO"))
            cards.append(
                "<div class='card'>"
                f"<h2>{cluster_name}</h2>"
                f"<p><strong>Status:</strong> <span class='{status.lower()}'>{status}</span></p>"
                f"<p><strong>Pass:</strong> {summary.get('pass_count', 0)} | "
                f"<strong>Warn:</strong> {summary.get('warn_count', 0)} | "
                f"<strong>Fail:</strong> {summary.get('fail_count', 0)}</p>"
                f"{self._format_results(summary)}"
                "</div>"
            )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>CloudHealth Combined Report</title>
            <style>
                body {{ font-family: sans-serif; background: #0f172a; color: white; padding: 2rem; }}
                .card {{ background: #1e293b; border-radius: 0.5rem; padding: 1.5rem; margin-bottom: 1rem; }}
                .pass {{ color: #10b981; }}
                .fail {{ color: #ef4444; }}
                .warn {{ color: #f59e0b; }}
                .info, .skip {{ color: #cbd5e1; }}
                .error {{ color: #fb7185; }}
                .check-item {{ margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid rgba(255,255,255,0.08); }}
                .check-message {{ margin-bottom: 0.3rem; }}
                .check-detail {{ color: #cbd5e1; white-space: pre-wrap; font-size: 0.92rem; }}
            </style>
        </head>
        <body>
            <h1>CloudHealth Combined Diagnostic Report</h1>
            <p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
            {''.join(cards) or '<div class="card">No cluster results available.</div>'}
        </body>
        </html>
        """
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        return report_path

    def _safe_filename_part(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", value or "")
        cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
        return cleaned or "cluster"

    def _format_results(self, results):
        # Simplified formatting for the mockup/beta
        sections = results.get('sections', [])
        html_parts = []
        for sec in sections:
            section_name = html.escape(sec.get("name", "Unnamed Section"))
            section_status = (sec.get("status") or "INFO").upper()
            status_class = section_status.lower()
            checks = sec.get("checks", [])
            check_html = []
            for item in checks:
                check_status = html.escape((item.get("status") or "INFO").upper())
                message = html.escape(item.get("message") or "")
                detail = item.get("detail")
                detail_html = ""
                if detail:
                    detail_html = f"<div class='check-detail'>{html.escape(str(detail))}</div>"
                check_html.append(
                    f"<div class='check-item'>"
                    f"<div class='check-message'><strong class='{check_status.lower()}'>{check_status}</strong> {message}</div>"
                    f"{detail_html}"
                    f"</div>"
                )
            html_parts.append(
                f"<div class='card'>"
                f"<h3>{section_name} - <span class='{status_class}'>{html.escape(section_status)}</span></h3>"
                f"{''.join(check_html) or '<div class=\"check-detail\">No check details recorded.</div>'}"
                f"</div>"
            )
        return "".join(html_parts)
