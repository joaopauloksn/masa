import scaling.agent as agent
import model_deployment.deploy as deploy


# def test_get_replicas():
#     replicas = agent.get_replicas("scaling", "user")
#     assert replicas == 1


# def test_scale_up():
#     assert agent.scale_up()


# def test_scale_down():
#     assert agent.scale_down()


# def test_deploy_model():
#     deploy.deploy_model()

# def test_get_current_status():
#     deploy.get_current_status()


def test_deploy_model():
    assert deploy.deploy_model()
