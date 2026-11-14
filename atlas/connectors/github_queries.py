"""GraphQL documents for the issue and git connectors.

Every document is a single ``query`` operation whose name is on the gateway allow-list.
"""

ISSUE_TIMELINE_TYPES = (
    "LABELED_EVENT",
    "UNLABELED_EVENT",
    "CLOSED_EVENT",
    "REOPENED_EVENT",
    "RENAMED_TITLE_EVENT",
    "CROSS_REFERENCED_EVENT",
    "MARKED_AS_DUPLICATE_EVENT",
    "UNMARKED_AS_DUPLICATE_EVENT",
    "CONNECTED_EVENT",
    "DISCONNECTED_EVENT",
    "REFERENCED_EVENT",
)

_TIMELINE_FRAGMENT = """
  __typename
  ... on LabeledEvent { createdAt label { name } }
  ... on UnlabeledEvent { createdAt label { name } }
  ... on ClosedEvent { createdAt stateReason closer { __typename ... on PullRequest { number } ... on Commit { oid } } }
  ... on ReopenedEvent { createdAt }
  ... on RenamedTitleEvent { createdAt previousTitle currentTitle }
  ... on CrossReferencedEvent { createdAt willCloseTarget source { __typename ... on Issue { number } ... on PullRequest { number } } }
  ... on MarkedAsDuplicateEvent { createdAt canonical { __typename ... on Issue { number } ... on PullRequest { number } } }
  ... on UnmarkedAsDuplicateEvent { createdAt }
  ... on ConnectedEvent { createdAt subject { __typename ... on Issue { number } ... on PullRequest { number } } }
  ... on DisconnectedEvent { createdAt subject { __typename ... on Issue { number } ... on PullRequest { number } } }
  ... on ReferencedEvent { createdAt commit { oid } }
"""

ISSUE_SNAPSHOT = """
query IssueSnapshot($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number title body createdAt closedAt state stateReason url
      author { login }
      labels(first: 50) { nodes { name } }
      userContentEdits(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { createdAt editedAt deletedAt diff } }
    }
  }
}
"""

ISSUE_TIMELINE = (
    """
query IssueTimeline($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(first: 100, after: $after, itemTypes: ["""
    + ", ".join(ISSUE_TIMELINE_TYPES)
    + """]) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes {"""
    + _TIMELINE_FRAGMENT
    + """}
      }
    }
  }
}
"""
)

ISSUE_COMMENTS = """
query IssueComments($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 50, after: $after) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes {
          databaseId createdAt body
          author { login }
          userContentEdits(first: 50) { totalCount nodes { createdAt editedAt deletedAt diff } }
        }
      }
    }
  }
}
"""

ISSUE_SEARCH = """
query IssueSearch($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue { number createdAt closedAt }
      ... on PullRequest { number createdAt mergedAt }
    }
  }
}
"""

PULL_REQUEST_SNAPSHOT = """
query PullRequestSnapshot($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title body createdAt mergedAt closedAt state url baseRefName
      author { login }
      mergeCommit { oid committedDate }
      labels(first: 30) { nodes { name } }
      files(first: 100) { totalCount nodes { path additions deletions } }
      userContentEdits(first: 50) { totalCount nodes { createdAt editedAt deletedAt diff } }
      timelineItems(first: 50, itemTypes: [RENAMED_TITLE_EVENT, LABELED_EVENT, UNLABELED_EVENT]) {
        nodes {
          __typename
          ... on RenamedTitleEvent { createdAt previousTitle currentTitle }
          ... on LabeledEvent { createdAt label { name } }
          ... on UnlabeledEvent { createdAt label { name } }
        }
      }
    }
  }
}
"""

CONTENT_EDITS_PROBE = """
query ContentEditsProbe($owner: String!, $name: String!, $issue: Int!, $pr: Int!) {
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $issue) { userContentEdits(first: 3) { totalCount nodes { editedAt diff } } }
    pullRequest(number: $pr) { userContentEdits(first: 3) { totalCount nodes { editedAt diff } } }
  }
}
"""

RATE_LIMIT = """
query RateLimit { rateLimit { remaining resetAt cost } }
"""
