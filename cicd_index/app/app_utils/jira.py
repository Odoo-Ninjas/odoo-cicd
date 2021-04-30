def _get_jira_wrapper(use_jira):
    if use_jira:
        jira_wrapper = JiraWrapper(
            os.environ['JIRA_URL'],
            os.environ['JIRA_USER'],
            os.environ['JIRA_PASSWORD'],
        )
    else:
        jira_wrapper = JiraWrapper("", "", "")
    return jira_wrapper

def augment_instance_from_jira(context, instance):
    title = 'n/a'
    creator = 'n/a'
    try:
        fields = context.jira_wrapper.infos(instance['git_branch'])
        if not isinstance(fields, dict):
            title = fields.summary
            creator = fields.creator.displayName
    except Exception as ex:
        logger.warn(ex)
    instance['title'] = title
    instance['initiator'] = creator

class JiraWrapper(object):
    def __init__(self, url, username, password):
        self.username = username
        self.password = password
        self.url = url

    def _jira(self):
        from jira.client import JIRA
        options = {'server': self.url}
        jira = JIRA(options, basic_auth=(self.username, self.password))
        return jira

    def _transform_key(self, storyKey):
        if storyKey.startswith("r2o"):
            storyKey = storyKey.upper()
        if storyKey.startswith("R2O_"):
            storyKey = storyKey.replace("R2O_", "R2O-")
        return storyKey

    def infos(self, storyKey):
        '''
        List of the properties that we may need

        :field assignee: return a User Object, Better do assignee.displayName
        :field created: Date of creation of the ticket
        :field creator: creator of the ticket,Object User :  creator.displayName
        :field customfield_10800: the field buildable (Instance) : .value returns None or Yes
        :field status:  Status of the Ticket
        :field summary: Title/Description of the ticket
        :return: The whole object that contains all of the issues properties
        :rtype: object
        '''
        if not self.url:
            return {}

        return self._jira().search_issues("key=" + self._transform_key(storyKey), maxResults=1)[0].fields

    def issue_is_buildable(self, storyKey):
        '''
        Check if the jira Ticket is assigned to be buildable
        to enable jenkins build an instance

        :rtype: object or None
        '''
        if not self.url:
            return {}
        return self.infos(storyKey).customfield_10800

    def comment(self, storyKey, message):
        if not self.url:
            return
        try:
            self._jira().add_comment(self._transform_key(storyKey), message)
        except Exception:
            print(f"ERROR: Could not send comment {storyKey} - {message}")
