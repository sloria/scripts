# /// script
# dependencies = [
#     "environs",
#     "httpx",
#     "loguru",
#     "click",
# ]
# ///
"""Migrate issues from labels to issue types on GitHub.

Requires GITHUB_TOKEN environment variable to be set.
Must be a token with repo scope.

To dry run:

    uv run scripts/migrate_to_issue_types.py marshmallow-code/marshmallow --dry

To migrate:

    uv run scripts/migrate_to_issue_types.py marshmallow-code/marshmallow
"""
import sys
import httpx
import click
import asyncio
from environs import Env
from loguru import logger

DEFAULT_LABEL_TO_TYPE_MAPPING = {
    'bug': 'Bug',
    'enhancement': 'Feature'
}

env = Env(eager=False)
env.read_env()
TOKEN = env.str("GITHUB_TOKEN")  # requires repo scope
LOG_LEVEL = env.log_level("LOG_LEVEL", "INFO")
LABEL_TO_TYPE_MAPPING = env.dict("LABEL_TO_TYPE_MAPPING", DEFAULT_LABEL_TO_TYPE_MAPPING)
env.seal()

logger.remove()
logger.add(sys.stderr, format="<level>{level}</level> {message}", level=LOG_LEVEL, colorize=True)


class GitHubIssueMigrator:
    def __init__(self, *, token: str, owner: str, name: str):
        self.token = token
        self.owner = owner
        self.name = name
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'GraphQL-Features': 'issue_types'
        }
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=30.0,
            follow_redirects=True
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    async def get_issues_with_label(self, label: str) -> list:
        query = """
        query($owner: String!, $name: String!, $label: String!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            issues(first: 100, labels: [$label], after: $cursor) {
              edges {
                node {
                  id
                  number
                  title
                  url
                }
              }
              pageInfo {
                endCursor
                hasNextPage
              }
            }
          }
        }
        """
        variables = {
            'owner': self.owner,
            'name': self.name,
            'label': label,
            'cursor': None
        }
        issues = []
        while True:
            logger.debug(f"Fetching issues with label '{label}'")
            response = await self.client.post(
                'https://api.github.com/graphql',
                json={'query': query, 'variables': variables}
            )
            response.raise_for_status()
            result = response.json()
            if 'errors' in result:
                raise httpx.HTTPError(f"GraphQL error: {result['errors']}")
            data = result['data']['repository']['issues']
            issues.extend(edge['node'] for edge in data['edges'])
            if not data['pageInfo']['hasNextPage']:
                break
            variables['cursor'] = data['pageInfo']['endCursor']
        return issues

    # See https://github.com/orgs/community/discussions/139933
    async def get_issue_type(self, issue_type_name: str) -> dict:
        query = """
        query($login: String!) {
          organization(login: $login) {
            issueTypes(first: 100) {
              nodes {
                id
                name
              }
            }
          }
        }
        """
        variables = {
            'login': self.owner
        }
        logger.debug(f"Fetching issue type ID for '{issue_type_name}'")
        response = await self.client.post(
            'https://api.github.com/graphql',
            json={'query': query, 'variables': variables}
        )
        response.raise_for_status()
        result = response.json()
        if 'errors' in result:
            raise httpx.HTTPError(f"GraphQL error: {result['errors']}")
        issue_types = result['data']['organization']['issueTypes']['nodes']
        for issue_type in issue_types:
            if issue_type['name'].lower() == issue_type_name.lower():
                return issue_type
        raise ValueError(f"Issue type '{issue_type_name}' not found")

    async def get_label_id(self, label_name: str) -> str:
        query = """
        query($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            labels(first: 100) {
              nodes {
                id
                name
              }
            }
          }
        }
        """
        variables = {
            'owner': self.owner,
            'name': self.name
        }
        logger.debug(f"Fetching label ID for '{label_name}'")
        response = await self.client.post(
            'https://api.github.com/graphql',
            json={'query': query, 'variables': variables}
        )
        response.raise_for_status()
        result = response.json()
        if 'errors' in result:
            raise httpx.HTTPError(f"GraphQL error: {result['errors']}")
        labels = result['data']['repository']['labels']['nodes']
        for label in labels:
            if label['name'].lower() == label_name.lower():
                return label['id']
        raise ValueError(f"Label '{label_name}' not found")

    # See https://github.com/orgs/community/discussions/139933
    async def update_issue_type(self, *, issue_id: str, issue_number: int, issue_type_id: str) -> None:
        query = """
        mutation($issueId: ID!, $issueTypeId: ID!) {
          updateIssueIssueType(input: {
            issueId: $issueId,
            issueTypeId: $issueTypeId
          }) {
            issue {
              id
            }
          }
        }
        """
        variables = {
            'issueId': issue_id,
            'issueTypeId': issue_type_id
        }
        logger.debug(f"Updating issue #{issue_number} to type ID '{issue_type_id}'")
        response = await self.client.post(
            'https://api.github.com/graphql',
            json={'query': query, 'variables': variables}
        )
        response.raise_for_status()
        result = response.json()
        if 'errors' in result:
            raise httpx.HTTPError(f"GraphQL error: {result['errors']}")

    async def remove_label(self, *, issue_id: str, issue_number: int, label_id: str) -> None:
        query = """
        mutation($issueId: ID!, $labelId: ID!) {
          removeLabelsFromLabelable(input: {
            labelableId: $issueId,
            labelIds: [$labelId]
          }) {
            clientMutationId
          }
        }
        """
        variables = {
            'issueId': issue_id,
            'labelId': label_id
        }
        logger.debug(f"Removing label ID '{label_id}' from issue #{issue_number}")
        response = await self.client.post(
            'https://api.github.com/graphql',
            json={'query': query, 'variables': variables}
        )
        response.raise_for_status()
        result = response.json()
        if 'errors' in result:
            raise httpx.HTTPError(f"GraphQL error: {result['errors']}")

    async def migrate_issues(self, limit: int | None = None, dry_run: bool = False) -> int:
        """Migrate issues from labels to issue types."""
        count = 0
        for label, issue_type_name in LABEL_TO_TYPE_MAPPING.items():
            issue_type = await self.get_issue_type(issue_type_name)
            label_id = await self.get_label_id(label)
            logger.info(f"Processing issues with label: {label}")
            issues = await self.get_issues_with_label(label)
            logger.info(f"Found {len(issues)} issues with label '{label}'")

            for issue in issues:
                if limit is not None and count >= limit:
                    logger.info(f"Reached limit of {limit} issues")
                    return count

                issue_id = issue['id']
                issue_number = issue['number']
                issue_url = issue['url']
                logger.info(f"Migrating issue {issue_url}")
                logger.info(f"  from label '{label}' to type '{issue_type['name']}'")
                try:
                    if not dry_run:
                        await self.update_issue_type(issue_id=issue_id, issue_number=issue_number, issue_type_id=issue_type['id'])
                        await self.remove_label(issue_id=issue_id, issue_number=issue_number, label_id=label_id)
                        logger.success(f"Successfully migrated issue #{issue_number}")
                    else:
                        logger.debug("Dry run, skipping migration")
                except httpx.HTTPError as e:
                    logger.error(f"Error migrating issue #{issue_number}: {str(e)}")
                finally:
                    count += 1
        return count

@click.command(help="Migrate issues from labels to issue types on GitHub.")
@click.argument("repo")
@click.option("--limit", default=None, type=int, help="Limit the number of issues to migrate")
@click.option("--dry", is_flag=True, help="Enable dry run mode")
def main(repo, limit, dry):
    owner, name = repo.split("/")
    async def run_migration():
        async with GitHubIssueMigrator(token=TOKEN, owner=owner, name=name) as migrator:
            count = await migrator.migrate_issues(limit=limit, dry_run=dry)
        logger.success(f"{'[DRY] ' if dry else ''}{count} issues migrated")
    asyncio.run(run_migration())

if __name__ == "__main__":
    main()
