import os, sys, fnmatch, numpy, logging, yaml, inspect, requests, boto3, time

from plumbum import colors
from invoke import run, Failure
from os import walk
from requests import ConnectionError, HTTPError
from time import sleep
from boto3.exceptions import Boto3Error
from botocore.exceptions import ClientError


# This might be bad...assuming that wherever this is running its always going to be
# TERM=ansi and up to 256 colors.
colors.use_color = 3


#
def ec2_compute_tags(nodename):
    # in addition to AWS_TAGS, include a tag for Docker version which will be
    # referenced by later provisining scripts.
    docker_version = str(os.environ['RANCHER_DOCKER_VERSION']).rstrip()
    tags = str(os.environ['AWS_TAGS']).rstrip()
    tags += ',rancher.docker.version,{}'.format(docker_version)
    tags += ',Name,{}'.format(nodename)
    return tag_csv_to_array(tags)


#
def aws_get_region():
    return str(os.environ['AWS_DEFAULT_REGION']).rstrip()


#
def sts_decode_auth_msg(codedmsg):
    try:
        decoded = boto3.client('sts').decode_authorization_message(EncodedMessage=codedmsg)
    except Boto3Error as e:
        msg = 'Failed while decoding STS auth msg!: {} :: {}'.format(codedmsg, str(e))
        log_debug(msg)
        raise RuntimeError(msg)

    return decoded


#
def nuke_aws_keypair(name):
    log_debug("Removing AWS key pair '{}'...".format(name))

    try:
        boto3.resource('ec2', region_name='us-west-2').KeyPair(name).delete()
    except Boto3Error as e:
        log_debug(str(e.message))
        raise RuntimeError(e.message) from e

    return True


#
def is_debug_enabled():
    if 'DEBUG' in os.environ and 'false' != os.environ.get('DEBUG'):
        return True
    else:
        return False


# Override the output of default logging.Formatter to instead use calling function/frame metadata
# and do other fancy stuff.
class FancyFormatter(logging.Formatter):
    def __init__(self):
        fmt = colors.dim | \
              '%(asctime)s - %(levelname)s - %(caller_filename)s:%(caller_lineno)s - %(caller_funcName)s - %(message)s' | \
              colors.fg.reset
        super(FancyFormatter, self).__init__(fmt=fmt)


# Logging setup.
# If debug mode is enabled, use customer RewindFormatter (see above).
log = logging.getLogger(__name__)
stream = logging.StreamHandler()

if is_debug_enabled():
    log.setLevel(logging.DEBUG)
    stream.setFormatter(FancyFormatter())
else:
    format = '%(asctime)s - %(levelname)s - %(message)s'
    log.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter(format))

log.addHandler(stream)


#
def run_with_retries(cmd, echo=False, sleep=10, attempts=10):
    current_attempts = 0
    result = None

    while current_attempts <= attempts:
        current_attempts += 1
        try:
            result = run(cmd, echo=echo)
            break
        except Failure as e:
            if current_attempts < attempts:
                msg = "Attempt {}/{} of {} failed. Sleeping for {}...".format(current_attempts, attempts, cmd)
                log_info(msg)
                sleep(sleep)
            else:
                msg = "Exceeded max attempts {} for {}!".format(attempts, cmd)
                log_debug(msg)
                raise Failure(msg) from e

    return result


#
def request_with_retries(method, url, data={}, step=10, attempts=10):

    timeout = 5
    response = None
    current_attempts = 0

    log_info("Sending request '{}' '{}'...".format(method, url))
    log_debug("Payload data: {}".format(data))

    while True:
        try:
            current_attempts += 1
            if 'PUT' == method:
                response = requests.put(url, timeout=timeout, json=data)
            elif 'GET' == method:
                response = requests.get(url, timeout=timeout)
            elif 'POST' == method:
                response = requests.post(url, timeout=timeout, json=data)
            else:
                log_error("Unsupported method \'{}\' specified!".format(method))
                return False

            log_info("response code: HTTP {}".format(response.status_code))
            log_debug("response: Headers:: {}".format(response.headers))

            # we might get a 200, 201, etc
            if not str(response.status_code).startswith('2'):
                response.raise_for_status()
            else:
                return response

        except (ConnectionError, HTTPError) as e:
            if current_attempts >= attempts:
                msg = "Exceeded max attempts. Giving up!: {}".format(str(e))
                log_debug(msg)
                raise Failure(msg) from e
            else:
                log_info("Request did not succeeed. Sleeping and trying again... : {}".format(str(e)))
                sleep(step)

    return response


#
def get_parent_frame_metadata(frame):
    parent_frame = inspect.getouterframes(frame, 2)

    return {
        'caller_filename': parent_frame[1].filename,
        'caller_lineno': parent_frame[1].lineno,
        'caller_funcName': parent_frame[1].function + "()"
    }


#
def log_info(msg):
    log.info(colors.fg.white | msg,
             extra=get_parent_frame_metadata(inspect.currentframe()))


#
def log_debug(msg):
    log.debug(colors.fg.lightblue & colors.dim | msg,
              extra=get_parent_frame_metadata(inspect.currentframe()))


#
def log_error(msg):
    log.error(colors.fatal | msg,
              extra=get_parent_frame_metadata(inspect.currentframe()))


#
def log_warn(msg):
    log.warn(colors.warn | msg,
             extra=get_parent_frame_metadata(inspect.currentframe()))


#
def claxon_and_exit(msg):
    log.error(colors.fatal | msg,
              extra=get_parent_frame_metadata(inspect.currentframe()))
    sys.exit(-10)


#
def log_success(msg=''):
    if '' is msg:
        msg = '[OK]'
    log.info(colors.fg.green & colors.bold | msg,
             extra=get_parent_frame_metadata(inspect.currentframe()))


#
def err_and_exit(msg):
    log.error(colors.fg.red & colors.bold | msg,
              extra=get_parent_frame_metadata(inspect.currentframe()))
    sys.exit(-1)


# Given the OS, return a dictionary of OS-specific setting values
# FIXME: Have this reference a config file for easy addtl platform support.
def os_to_settings(os):
    if 'ubuntu-1604' in os:
        ami = 'ami-a9d276c9'
        ssh_username = 'ubuntu'

    elif 'ubuntu-1404' in os:
        ami = 'ami-01f05461'
        ssh_username = 'ubuntu'

    elif 'centos-7' in os:
        ami = 'ami-d2c924b2'
        ssh_username = 'centos'

    elif 'rhel-7' in os:
        ami = 'ami-6f68cf0f'
        ssh_username = 'ec2-user'

    elif 'rhel-7.2' in os:
        ami = 'ami-775e4f16'
        ssh_username = 'ec2-user'

    elif 'rancheros-v07' in os:
        ami = 'ami-2c13934c'
        ssh_username = 'rancher'

    elif 'coreos-stable' in os:
        ami = 'ami-06af7f66'
        ssh_username = 'core'

    else:
        raise RuntimeError("Unsupported OS specified '{}'!".format(os))

    return {'ami-id': ami, 'ssh_username': ssh_username}


#
def ec2_wait_for_state(instance, desired_state, timeout=300):
    log_info("Waiting for node '{}' to enter state '{}'...".format(instance, desired_state))

    steptime = 5
    actual_state = None
    nodefilter = [{'Name': 'instance-id', 'Values': [instance]}]
    ec2 = boto3.client('ec2', region_name=aws_get_region())

    starttime = time.time()
    while time.time() - starttime < timeout:
        try:
            rez = ec2.describe_instances(Filters=nodefilter)['Reservations']
            log_debug("rez: {}".format(rez))

            if 0 < len(rez):
                actual_state = rez[0]['Instances'][0]['State']['Name']
                log_debug("desired state: {} ; actual state: {}".format(desired_state, actual_state))

                if actual_state == desired_state:
                    log_info("Node '{}' has entered state '{}'.".format(instance, desired_state))
                    break
                else:
                    sleep(steptime)
            else:
                log_debug("Not yet able to query instance state...")
                sleep(steptime)

        except Boto3Error as e:
            msg = "Failed while querying instance '{}' state!: {}".format(instance, str(e))
            log_debug(msg)
            raise RuntimeError(msg)


#
def ec2_tag_value(nodename, tagname):
    log_debug("Looking up tag '{}' for instance '{}'...".format(tagname, nodename))

    tagvalue = None

    try:
        ec2_filter = [{'Name': 'tag:Name', 'Values': [nodename]}]
        log_debug("tag filter: {}".format(ec2_filter))

        ec2 = boto3.client('ec2')
        node_metadata = ec2.describe_instances(Filters=ec2_filter)
        log_debug("node metadata: {}".format(node_metadata))

        tags = node_metadata['Reservations'][0]['Instances'][0]['Tags']
        log_debug("tags: {}".format(tags))

        for tag in tags:
            if tagname == tag['Key']:
                tagvalue = tag['Value']
                break

    except (IndexError, KeyError, Boto3Error) as e:
        msg = "Failed while looking up tag '{}'!: {}".format(tagname, str(e))
        log_debug(msg)
        raise RuntimeError(msg) from e

    return tagvalue


#
def ec2_instance_id_from_name(name):
    log_debug("Getting metadata for '{}'...".format(name))

    iid = None
    name_filter = [{'Name': 'tag:Name', 'Values': name}]
    ec2 = boto3.client('ec2')

    try:
        iid = ec2.describe_instances(Filters=name_filter)['Reservations'][0]['Instances'][0]['InstanceId']
    except Boto3Error as e:
        msg = "Failed while querying instance-id for name '{}'! :: {}".format(name, e.message)
        log_debug(msg)
        raise RuntimeError(msg) from e

    return iid


#
def aws_volid_from_tag(name):
    log_debug("Getting volid for non-root volume on instance '{}'...".format(name))

    volid = None

    try:
        volid = ec2_tag_value(name, 'rancherlabs.ci.addtl_volume')
    except RuntimeError as e:
        msg = "Failed to get volid for non-root volume!: {}".format(str(e))
        log_debug(msg)
        raise RuntimeError(msg) from e

    return volid


#
def ebs_deprovision_volume(name, region='us-west-2', zone='a'):
    log_info("Removing volume '{}' if  it exists...".format(name))

    try:
        vol_filter = [{'Name': 'tag:Name', 'Values': [name]}]
        log_debug("vol filter: {}".format(vol_filter))
        ec2 = boto3.client('ec2')
        vols = ec2.describe_volumes(Filters=vol_filter)
        log_debug("Volumes to delete: {}".format(vols))

        if 0 != len(vols['Volumes']):
            for vol in range(0, len(vols['Volumes'])):
                volid = vols['Volumes'][vol]['VolumeId']
                log_debug("Deleteting vol [{}] : id '{}'...".format(vol, volid))
                ec2.delete_volume(VolumeId=volid)

    except Boto3Error as e:
        msg = "Failed deprovisioning EBS volume..."
        log_debug(msg)
        raise RuntimeError(msg) from e

    return True


#
def tag_csv_to_array(tagcsv):
    log_debug("Converting tag csv to array: {}".format(tagcsv))

    tag_dict_list = []
    taglist = tagcsv.split(',')
    taglist.reverse()

    if 0 != len(taglist) % 2:
        msg = "AWS_TAGS split on ',' has length {} which makes no sense!".format(str(len(taglist)))
        log_debug(msg)
        raise RuntimeError(msg)

    while 0 != len(taglist):
        tag_dict = {'Key': str(taglist.pop()), 'Value': str(taglist.pop())}
        tag_dict_list.append(tag_dict)

    return tag_dict_list


#
def ebs_provision_volume(name, region='us-west-2', zone='a', size=20, voltype='gp2', tags='is_ci,true'):
    log_info("Creating EBS volume...")

    try:
        ec2 = boto3.resource('ec2', region_name=region)
        log_debug("Creating EBS volume '{}'...".format(name))
        vol = ec2.create_volume(Size=size, VolumeType=voltype, AvailabilityZone="{}{}".format(region, zone))
        log_info("EBS volume '{}' created...".format(str(vol.id)))

        tags = tag_csv_to_array(tags)
        log_info("Tagging volume '{}' : '{}'...".format(str(vol.id), tags))
        ec2.create_tags(Resources=[vol.id], Tags=tags)

    except (RuntimeError, Boto3Error) as e:
        msg = "Failed while provisioning EBS volme!: {}".format(e.message)
        log_debug(msg)
        raise RuntimeError(msg) from e

    return vol.id


#
def aws_to_dm_env():
    log_debug('Performing envvar translation from AWS to Docker Machine...')

    # inject some EC2 tags we're going to need later
    docker_version_tag = "rancher.docker.version,{}".format(os.environ['RANCHER_DOCKER_VERSION'])
    os.environ['AWS_TAGS'] = "{},{}".format(os.environ['AWS_TAGS'], docker_version_tag)

    aws_params = {k: v for k, v in os.environ.items() if k.startswith('AWS')}
    for k, v in aws_params.items():
        newk = k.replace('AWS_', 'AMAZONEC2_')
        os.environ[newk] = v.rstrip(os.linesep)

    # cover the cases where direct translation of names is not consistent
    os.environ['AMAZONEC2_ACCESS_KEY'] = os.environ['AWS_ACCESS_KEY_ID']
    os.environ['AMAZONEC2_SECRET_KEY'] = os.environ['AWS_SECRET_ACCESS_KEY']
    os.environ['AMAZONEC2_REGION'] = os.environ['AWS_DEFAULT_REGION']

    log_debug("Docker Machine envvars are: {}".format(run("env | egrep 'AMAZONEC2_'", echo=False, hide=True).stdout))

    return True


#
def find_files(rootdir, pattern, excludes=[]):
    """
    Recursive find of files matching pattern starting at location of this script.

    Args:
      rootdir (str): where to scart file name matching
      pattern (str): filename pattern to match
      excludes: array of patterns for to exclude from find

    Returns:
      array: list of matching files
    """
    matches = []
    DEBUG = False

    try:
        log_debug("Search for pattern \'{}\' from root of '{}\'...".format(pattern, rootdir))

        for root, dirnames, filenames in walk(rootdir):
            for filename in fnmatch.filter(filenames, pattern):
                matches.append(os.path.join(root, filename))

        # Oh, lcomp sytnax...
        for exclude in excludes:
            matches = numpy.asarray(
                [match for match in matches if exclude not in match])

        log_debug("Matches in find_files is : {}".format(str(matches)))

    except FileNotFoundError as e:
        log_error("Failed to chdir to \'{}\': {} :: {}".format(e.errno, e.strerror))
        return False

    return matches


#
def lint_check(rootdir, filetypes=[], excludes=[]):

    default_filetypes = ['py', 'pp', 'rb']
    result = True

    # if someone passes a non-list then cast it to a list
    if not isinstance(filetypes, list):
        filetypes = [filetypes]

    if [] is filetypes:
        filetypes = default_filetypes

    else:
        for specified_type in filetypes:
            if specified_type not in default_filetypes:
                log_error("Sorry, do not provide lint checking for filetype \'{}\'.".format(specified_type))
                result = False

        if False is result:
            return False

    for filetype in filetypes:
        filetype = '*.' + filetype

        found_files = find_files(rootdir, filetype, excludes)
        if False is found_files:
            log_error("Error during lint check for files matching \'{}\'!")
            return False

        else:
            if len(found_files) > 0:

                # figure out which command we need to run to do a lint check
                cmd = ''
                if '*.py' == filetype:
                    cmd = "flake8 --statistics --show-source --max-line-length=160 --ignore={} {}".format(
                        'E111,E114,E122,E401,E402,E266,F841,E126,E501',
                        ' '.join(found_files))

                elif '*.pp' == filetype:
                    cmd = "puppet-lint {}".format(' '.join(found_files))

                elif '*.rb' == filetype:
                    cmd = "ruby-lint {}".format(' '.join(found_files))

#                cmd = cmd.format(' '.join(found_files))
                log_debug("Lint checking \'{}\'...".format(' '.join(found_files)))
                if is_debug_enabled():
                    run(cmd, echo=True)
                else:
                    run(cmd)

    return True


#
def syntax_check(rootdir, filetypes=[], excludes=[]):

    default_filetypes = ['sh', 'py', 'yaml', 'pp', 'rb']
    result = True

    # if someone passes a non-list then cast it to a list
    if not isinstance(filetypes, list):
        filetypes = [filetypes]

    if [] is filetypes:
        filetypes = default_filetypes

    else:
        for specified_type in filetypes:
            if specified_type not in default_filetypes:
                log_error("Sorry, do not provide syntax checking for filetype \'{}\'.".format(specified_type))
                result = False

        if False is result:
            return False

    try:
        for filetype in filetypes:
            filetype = '*.' + filetype

            found_files = find_files(rootdir, filetype, excludes)
            if False is found_files:
                log_error("Error during syntax check for files matching \'{}\'!")
                return False

            else:
                if len(found_files) > 0:
                    # figure out which command we need to run to do a syntax check
                    cmd = ''
                    if '*.sh' == filetype:
                        cmd = "bash -n {}"

                    elif '*.py' == filetype:
                        cmd = "python -m py_compile {}"

                    elif '*.pp' == filetype:
                        cmd = "puppet parser validate {}"

                    elif '*.rb' == filetype:
                        cmd = "ruby -c {}"

                    # do the syntax check
                    if '*.yaml' == filetype or '*.yaml' == filetype:
                        for found_file in found_files:
                            log_debug("Syntax checking \'{}\' via Python yaml.load()...".format(found_file))
                            yaml.load(found_file)
                    else:
                        cmd = cmd.format(' '.join(found_files))
                        log_debug("Syntax checking \'{}\'...".format(' '.join(found_files)))
                        if is_debug_enabled():
                            run(cmd, echo=True)
                        else:
                            run(cmd)

    except (yaml.YAMLError, Failure) as e:
        err_and_exit(str(e))

    return True


#
def ec2_ensure_ssh_keypair(nodename):
    log_debug('Ensuring a ssh keypair exists...')

    try:
        # create a key pair in the filesystem if one does not already exist
        if not os.path.isfile('.ssh/{}'.format(nodename)):
            run('mkdir -p .ssh && rm -rf .ssh/{}'.format(nodename), echo=True)
            run("ssh-keygen -N '' -C '{}' -f .ssh/{}".format(nodename, nodename), echo=True)
            run("chmod 0600 .ssh/{}".format(nodename), echo=True)

        # update the key pair in AWS - Yes, Terraform has a Provider for this and Pupupet does not...
        log_info("Uploading ssh pub key '{}' to AWS...".format(nodename))
        ec2 = boto3.client('ec2', region_name=str(os.environ['AWS_DEFAULT_REGION']).rstrip())
        ec2.delete_key_pair(KeyName=nodename)

        pubkey = open('.ssh/{}.pub'.format(nodename), 'r').read()
        log_debug("pub key: '{}'".format(pubkey))

        #                        WTF??!? Docs say this has to b64 encoded!?!?
        #                        b64pubkey = base64.b64encode(bytes(pubkey, 'utf-8').ascii())
        #                        log_debug("base64 pub key: '{}'".format(b64pubkey))

        ec2.import_key_pair(
            KeyName=nodename,
            PublicKeyMaterial=pubkey)

    except (Failure, Boto3Error) as e:
        msg = "Failed while ensuring ssh keypair!: {}".format(str(e))
        log_debug(msg)
        raise RuntimeError(msg) from e

    return nodename


#
def ec2_node_ensure(nodename, instance_type='m4.large'):
    log_info("Ensuring node '{}'...".format(nodename))

    server_os = str(os.environ['RANCHER_SERVER_OPERATINGSYSTEM']).rstrip()
    os_settings = os_to_settings(server_os)
    sgids = [str(os.environ['AWS_SECURITY_GROUP_ID']).rstrip()]
    zone = str(os.environ['AWS_ZONE']).rstrip()
    region = str(os.environ['AWS_DEFAULT_REGION']).rstrip()
    placement = {'AvailabilityZone': '{}{}'.format(region, zone)}
    subnetid = str(os.environ['AWS_SUBNET_ID']).rstrip()
    region = str(os.environ['AWS_DEFAULT_REGION']).rstrip()

    custom_vols = None

    network_ifs = [{
        'DeviceIndex': 0,
        'SubnetId': subnetid,
        'AssociatePublicIpAddress': True,
        'Groups': sgids,
    }]

    # only intersted in nodes which might have same name and which are running or pending
    node_filter = [
        {'Name': 'tag:Name', 'Values': [nodename]},
        {'Name': 'instance-state-name', 'Values': ['running', 'pending']}
    ]

    try:
        ec2 = boto3.client('ec2', region_name=region)
        instances = ec2.describe_instances(Filters=node_filter)
        log_debug("instance: {}".format(instances))

        # first check if server(s) by our specified name already exists
        if 0 != len(instances['Reservations']):
            msg = "Detected already running instance by name of '{}'...".format(nodename)
            log_debug(msg)
            raise RuntimeError(msg)

        # nope, let's go ahead and create one
        else:
            keyname = ec2_ensure_ssh_keypair(nodename)

            # yuck
            iam_profile = boto3.resource('iam').InstanceProfile(str(os.environ['AWS_INSTANCE_PROFILE']))
            iam_profile = {'Name': iam_profile.name}

            # CoreOS is odd-ball in that it uses a different root volume
            if 'core' in server_os:
                custom_vols = [{'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 30}}]
                log_info("Setting custom root device '{}' for CoreOS...".format(custom_vols))

            # RHEL osfamily needs a second LVM volume for thinpool config
            if 'rhel' in server_os or 'centos' in server_os:
                custom_vols = [{
                    'DeviceName': '/dev/sdb',
                    'Ebs': {'VolumeSize': 30, 'DeleteOnTermination': True}}]
                log_info("Creating second volume to host thinpool config for RHEL osfamily: {}".format(custom_vols))

            log_info("Creating Rancher Server '{}'...".format(nodename))

            # have to include block device mapping configs for these OSes and setting
            # the parameter to None makes the boto3 API unhappy. :\
            if 'rhel' in server_os or 'centos' in server_os or 'core' in server_os:
                instance = ec2.request_spot_instances(
                    SpotPrice='0.10',
                    InstanceCount=1,
                    Type='one-time',
                    LaunchSpecification={
                        ImageId=os_settings['ami-id'],
                        KeyName=keyname,
                        InstanceType=instance_type,
                        Placement=placement,
                        NetworkInterfaces=network_ifs,
                        IamInstanceProfile=iam_profile,
                        BlockDeviceMappings=custom_vols
                    })
                    
            else:
                instance = ec2.request_spot_instances(
                    SpotPrice='0.10',
                    InstanceCount=1,
                    Type='one-time',
                    LaunchSpecification={
                        ImageId=os_settings['ami-id'],
                        KeyName=keyname,
                        InstanceType=instance_type,
                        Placement=placement,
                        NetworkInterfaces=network_ifs,
                        IamInstanceProfile=iam_profile,
                    })

            log_debug("run request response for '{}'...".format(instance))
            log_debug("instance info: {}".format(instance['Instances']))

            instance_id = instance['Instances'][0]['InstanceId']
            log_info("instance-id of Rancher Server node: {}".format(instance_id))

            # we have to sleep for a bit before we start asking for node metadata
            time.sleep(20)

            tags = ec2_compute_tags(nodename)
            log_info("Tagging instance '{}' with tags: {}".format(instance_id, tags))

            # give our instance time to enter 'pending' before we try to tag it
            ec2.create_tags(
                Resources=[instance_id],
                Tags=tags)

        # waiting for 'running' is the easiest way to eliminate race conditions later
        log_info("Waiting for node to enter state 'running'...")
        ec2_wait_for_state(instance_id, 'running')

        public_ip = ec2_node_public_ip(nodename, region)
        log_info("Node '{}' is available at address '{}'.".format(nodename, public_ip))

    except (ClientError, Boto3Error) as e:
        addtl_msg = str(e)
        if 'ClientError' == e.__class__.__name__:
            errmsg = e.response['Error']['Message']
            if 'Encoded authorization failure' in errmsg:
                codedmsg = errmsg.split(':')[1].replace(' ', '')
                addtl_msg = sts_decode_auth_msg(codedmsg)

        msg = "Failed while provisioning Rancher Server!: {}".format(addtl_msg)
        log_debug(msg)
        raise RuntimeError(msg) from e

    return True


#
def ec2_node_public_ip(nodename, region='us-west-2'):

    node_filter = [
        {'Name': 'tag:Name', 'Values': [nodename]},
        {'Name': 'instance-state-name', 'Values': ['running', 'pending']}
    ]

    try:
        ec2 = boto3.client('ec2', region_name=region)
        instances = ec2.describe_instances(Filters=node_filter)
        rez = instances['Reservations']
        log_debug("reservations: {}".format(rez))

        if len(rez) > 1:
            raise RuntimeError("Detected more than one reservation matching the filter. That's a problem!")
        else:
            pubip = str(rez[0]['Instances'][0]['PublicIpAddress'])

    except (ClientError, Boto3Error) as e:
        msg = "Failed while getting public IP address for node '{}'!: {}".format(nodename, str(e))
        log_debug(msg)
        raise RuntimeError(msg) from e

    return pubip


#
def ec2_node_terminate(nodename, region='us-west-2'):
    log_info("Terminating instance '{}'..".format(nodename))

    node_filter = [
        {'Name': 'tag:Name', 'Values': [nodename]},
        {'Name': 'instance-state-name', 'Values': ['running', 'pending']}
    ]

    try:
        ec2 = boto3.client('ec2', region_name=region)
        rez = ec2.describe_instances(Filters=node_filter)['Reservations']

        for node in range(0, len(rez)):
            instance_id = rez[0]['Instances'][node]['InstanceId']
            log_info("Terminated instance-id '{}'...".format(instance_id))
            ec2.terminate_instances(InstanceIds=[instance_id])

    except Boto3Error as e:
        msg = "Failed while terminating node '{}'!: {}".format(nodename, str(e))
        log_debug(msg)
        raise RuntimeError(msg) from e
